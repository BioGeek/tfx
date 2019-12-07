# Copyright 2019 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""TFMA benchmark."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time

# Standard Imports

from absl import flags
import apache_beam as beam
from apache_beam.runners.portability import fn_api_runner
import tensorflow_model_analysis as tfma
from tensorflow_model_analysis.eval_saved_model import load

from tensorflow.python.platform import test  # pylint: disable=g-direct-tensorflow-import
from tfx.benchmarks import benchmark_utils

FLAGS = flags.FLAGS

# Maximum number of examples to read from the dataset.
# TFMA is much slower than TFT, so we may have to read a smaller subset of the
# dataset.
MAX_NUM_EXAMPLES = 100000


class TFMABenchmark(test.Benchmark):
  """TFMA benchmark."""

  def __init__(self):
    super(TFMABenchmark, self).__init__()
    self.dataset = benchmark_utils.get_dataset(FLAGS.dataset)

  def benchmarkMiniPipeline(self):
    """Benchmark a "mini" version of TFMA - predict, slice and compute metrics.

    Runs a "mini" version of TFMA in a Beam pipeline. Records the time taken
    (as reported by Beam) for the PredictExtractor, SliceKeyExtractor and
    ComputeMetricsAndPlots stages, as well as the wall time taken for the whole
    pipeline.
    """
    pipeline = beam.Pipeline(runner=fn_api_runner.FnApiRunner())
    raw_data = (
        pipeline
        | "Examples" >> beam.Create(
            self.dataset.read_raw_dataset(
                deserialize=False, limit=MAX_NUM_EXAMPLES))
        | "InputsToExtracts" >> tfma.InputsToExtracts())

    eval_shared_model = tfma.default_eval_shared_model(
        eval_saved_model_path=self.dataset.tfma_saved_model_path())

    _ = (
        raw_data
        | "PredictExtractor" >> tfma.extractors.PredictExtractor(
            eval_shared_model=eval_shared_model).ptransform
        | "SliceKeyExtractor" >> tfma.extractors.SliceKeyExtractor().ptransform
        | "ComputeMetricsAndPlots" >> tfma.evaluators.MetricsAndPlotsEvaluator(
            eval_shared_model=eval_shared_model).ptransform)

    start = time.time()
    result = pipeline.run()
    result.wait_until_finish()
    end = time.time()
    delta = end - start

    benchmark_utils.report_beam_stages(
        self,
        name=benchmark_utils.with_dataset_prefix("benchmarkMiniPipeline",
                                                 FLAGS.dataset),
        stages=[
            "PredictExtractor", "SliceKeyExtractor", "ComputeMetricsAndPlots"
        ],
        infos=result.metrics().monitoring_infos(),
        extras={"num_examples": self.dataset.num_examples()})
    self.report_benchmark(
        name=benchmark_utils.with_dataset_prefix("benchmarkMiniPipeline",
                                                 FLAGS.dataset),
        iters=1,
        wall_time=delta,
        extras={
            "num_examples": self.dataset.num_examples(limit=MAX_NUM_EXAMPLES)
        })

  def benchmarkPredictAndAggregateCombineManualActuation(self):
    """Benchmark the predict and aggregate combine stages "manually".

    Runs _TFMAPredictionDoFn and _AggregateCombineFn "manually" outside a Beam
    pipeline. Records the wall time taken for each.
    """
    # Predict and AggregateCombine are both benchmarked in this single benchmark
    # to allow us to reuse the output of Predict. The alternative is to have a
    # separate benchmark for AggregateCombine where we duplicate the code for
    # running the predict stage.

    # Run InputsToExtracts manually.
    records = []
    for x in self.dataset.read_raw_dataset(
        deserialize=False, limit=MAX_NUM_EXAMPLES):
      records.append({tfma.constants.INPUT_KEY: x})

    fn = tfma.extractors.predict_extractor._TFMAPredictionDoFn(  # pylint: disable=protected-access
        eval_shared_model=tfma.default_eval_shared_model(
            eval_saved_model_path=self.dataset.tfma_saved_model_path()))
    fn.setup()

    # Predict
    predict_batch_size = 1000
    predict_result = []
    start = time.time()
    for batch in benchmark_utils.batched_iterator(records, predict_batch_size):
      predict_result.extend(fn.process(batch))
    end = time.time()
    delta = end - start
    self.report_benchmark(
        name=benchmark_utils.with_dataset_prefix(
            "benchmarkPredictAndAggregateCombineManualActuation.Predict",
            FLAGS.dataset),
        iters=1,
        wall_time=delta,
        extras={
            "batch_size": predict_batch_size,
            "num_examples": self.dataset.num_examples(limit=MAX_NUM_EXAMPLES)
        })

    # AggregateCombineFn
    #
    # We simulate accumulating records into multiple different accumulators,
    # each with inputs_per_accumulator records, and then merging the resulting
    # accumulators together at one go.

    # Number of elements to feed into a single accumulator.
    # (This means we will have len(records) / inputs_per_accumulator
    # accumulators to merge).
    inputs_per_accumulator = 1000

    combiner = tfma.evaluators.aggregate._AggregateCombineFn(  # pylint: disable=protected-access
        eval_shared_model=tfma.default_eval_shared_model(
            eval_saved_model_path=self.dataset.tfma_saved_model_path()))
    accumulators = []

    start = time.time()
    for batch in benchmark_utils.batched_iterator(predict_result,
                                                  inputs_per_accumulator):
      accumulator = combiner.create_accumulator()
      for elem in batch:
        combiner.add_input(accumulator, elem)
      accumulators.append(accumulator)
    final_accumulator = combiner.merge_accumulators(accumulators)
    final_output = combiner.extract_output(final_accumulator)
    end = time.time()
    delta = end - start

    # Extract output to sanity check example count. This is not timed.
    extract_fn = tfma.evaluators.aggregate._ExtractOutputDoFn(  # pylint: disable=protected-access
        eval_shared_model=tfma.default_eval_shared_model(
            eval_saved_model_path=self.dataset.tfma_saved_model_path()))
    extract_fn.setup()
    interpreted_output = list(extract_fn.process(((), final_output)))
    if len(interpreted_output) != 1:
      raise ValueError("expecting exactly 1 interpreted output, got %d" %
                       (len(interpreted_output)))
    got_example_count = interpreted_output[0][1].get(
        "post_export_metrics/example_count")
    if got_example_count != self.dataset.num_examples(limit=MAX_NUM_EXAMPLES):
      raise ValueError("example count mismatch: expecting %d got %d" %
                       (self.dataset.num_examples(limit=MAX_NUM_EXAMPLES),
                        got_example_count))

    self.report_benchmark(
        name=benchmark_utils.with_dataset_prefix(
            "benchmarkPredictAndAggregateCombineManualActuation"
            ".AggregateCombine", FLAGS.dataset),
        iters=1,
        wall_time=delta,
        extras={
            "inputs_per_accumulator": inputs_per_accumulator,
            "num_examples": self.dataset.num_examples(limit=MAX_NUM_EXAMPLES)
        })

  def benchmarkEvalSavedModelPredict(self):
    """Benchmark using the EvalSavedModel to make predictions.

    Runs EvalSavedModel.predict_list and records the wall time taken.
    """
    batch_size = 1000

    eval_saved_model = load.EvalSavedModel(
        path=self.dataset.tfma_saved_model_path(), include_default_metrics=True)

    records = self.dataset.read_raw_dataset(
        deserialize=False, limit=MAX_NUM_EXAMPLES)

    start = time.time()
    for batch in benchmark_utils.batched_iterator(records, batch_size):
      eval_saved_model.predict_list(batch)
    end = time.time()
    delta = end - start
    self.report_benchmark(
        name=benchmark_utils.with_dataset_prefix(
            "benchmarkEvalSavedModelPredict", FLAGS.dataset),
        iters=1,
        wall_time=delta,
        extras={
            "batch_size": batch_size,
            "num_examples": self.dataset.num_examples(limit=MAX_NUM_EXAMPLES)
        })

  def benchmarkEvalSavedModelMetricsResetUpdateGetList(self):
    """Benchmark using the EvalSavedModel to compute metrics.

    Runs EvalSavedModel.metrics_reset_update_get_list and records the wall time
    taken.
    """
    batch_size = 1000

    eval_saved_model = load.EvalSavedModel(
        path=self.dataset.tfma_saved_model_path(), include_default_metrics=True)

    records = self.dataset.read_raw_dataset(
        deserialize=False, limit=MAX_NUM_EXAMPLES)

    start = time.time()
    accumulators = []
    for batch in benchmark_utils.batched_iterator(records, batch_size):
      accumulators.append(eval_saved_model.metrics_reset_update_get_list(batch))
    end = time.time()
    delta = end - start

    # Sanity check
    metric_variables_sum = accumulators[0]
    for acc in accumulators[1:]:
      if len(metric_variables_sum) != len(acc):
        raise ValueError(
            "all metric variable value lists should have the same length, but "
            "got lists with different lengths: %d and %d" %
            (len(metric_variables_sum), len(acc)))
      metric_variables_sum = [a + b for a, b in zip(metric_variables_sum, acc)]

    metrics = eval_saved_model.metrics_set_variables_and_get_values(
        metric_variables_sum)
    if "average_loss" not in metrics:
      raise ValueError(
          "metrics should contain average_loss metric, but it did not. "
          "metrics were: %s" % metrics)

    self.report_benchmark(
        name=benchmark_utils.with_dataset_prefix(
            "benchmarkEvalSavedModelMetricsResetUpdateGetList", FLAGS.dataset),
        iters=1,
        wall_time=delta,
        extras={
            "batch_size": batch_size,
            "num_examples": self.dataset.num_examples(limit=MAX_NUM_EXAMPLES)
        })


if __name__ == "__main__":
  flags.DEFINE_string("dataset", "chicago_taxi", "Dataset to run on.")
  test.main()
