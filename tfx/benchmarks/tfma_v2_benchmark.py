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
"""TFMA v2 benchmark."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time

# Standard Imports

from absl import flags
import apache_beam as beam
from apache_beam.runners.portability import fn_api_runner

import tensorflow as tf
import tensorflow_model_analysis as tfma
from tensorflow_model_analysis import model_util as tfma_model_util
from tensorflow_model_analysis import types as tfma_types
from tensorflow_model_analysis.evaluators import metrics_and_plots_evaluator_v2
from tensorflow_model_analysis.extractors import input_extractor
from tensorflow_model_analysis.extractors import predict_extractor_v2
from tensorflow_model_analysis.metrics import metric_specs

from tensorflow.python.platform import test  # pylint: disable=g-direct-tensorflow-import
from tfx.benchmarks import benchmark_utils

FLAGS = flags.FLAGS

# Maximum number of examples to read from the dataset.
# TFMA is much slower than TFT, so we may have to read a smaller subset of the
# dataset.
MAX_NUM_EXAMPLES = 100000


# TODO(b/147827582): Also add "TF-level" Keras benchmarks for how TFMAv2
# gets predictions / computes metrics.
class TFMAV2Benchmark(test.Benchmark):
  """TFMA benchmark."""

  def __init__(self):
    super(TFMAV2Benchmark, self).__init__()
    tf.compat.v1.enable_v2_behavior()
    self.dataset = benchmark_utils.get_dataset(FLAGS.dataset)

    self._eval_config = tfma.config.EvalConfig(
        model_specs=[tfma.config.ModelSpec(label_key="tips")],
        metrics_specs=metric_specs.example_count_specs())
    self._model_loader = tfma_types.ModelLoader(
        tags=[tf.saved_model.SERVING],
        construct_fn=tfma_model_util.model_construct_fn(
            eval_saved_model_path=self.dataset.trained_saved_model_path(),
            tags=[tf.saved_model.SERVING]))

  def benchmarkMiniPipeline(self):
    """Benchmark a "mini" version of TFMA - predict, slice and compute metrics.

    Runs a "mini" version of TFMA in a Beam pipeline. Records the time taken
    (as reported by Beam) for the PredictExtractor, SliceKeyExtractor and
    MetricsAndPlotsEvaluator stages, as well as the wall time taken for the
    whole pipeline.
    """
    pipeline = beam.Pipeline(runner=fn_api_runner.FnApiRunner())
    raw_data = (
        pipeline
        | "Examples" >> beam.Create(
            self.dataset.read_raw_dataset(
                deserialize=False, limit=MAX_NUM_EXAMPLES))
        | "InputsToExtracts" >> tfma.InputsToExtracts())

    eval_shared_model = tfma_types.EvalSharedModel(
        model_path=self.dataset.trained_saved_model_path(),
        model_loader=self._model_loader)

    _ = (
        raw_data
        | "InputExtractor" >>
        input_extractor.InputExtractor(eval_config=self._eval_config).ptransform
        | "V2PredictExtractor" >> predict_extractor_v2.PredictExtractor(
            eval_config=self._eval_config,
            eval_shared_model=eval_shared_model).ptransform
        | "SliceKeyExtractor" >> tfma.extractors.SliceKeyExtractor().ptransform
        | "V2ComputeMetricsAndPlots" >>
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=self._eval_config,
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
            "V2PredictExtractor", "SliceKeyExtractor",
            "V2ComputeMetricsAndPlots"
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


if __name__ == "__main__":
  flags.DEFINE_string("dataset", "chicago_taxi", "Dataset to run on.")
  test.main()
