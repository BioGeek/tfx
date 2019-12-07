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
"""Utility functions shared across the different benchmarks."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import importlib


def with_dataset_prefix(name, dataset):
  """Returns a benchmark name with the dataset name prefixed."""
  return "dataset[%s].%s" % (dataset, name)


def get_dataset(name):
  """Imports the given dataset and returns an instance of it."""
  lib = importlib.import_module("..datasets.%s.dataset" % name, __name__)
  return lib.get_dataset()


def batched_iterator(records, batch_size):
  """Groups elements in the given list into batches of the given size.

  Args:
    records: List of elements to batch.
    batch_size: Size of each batch.

  Yields:
    Lists with batch_size elements from records. Every list yielded except the
    last will contain exactly batch_size elements.
  """
  batch = []
  for i, x in enumerate(records):
    batch.append(x)
    if (i + 1) % batch_size == 0:
      yield batch
      batch = []
  if batch:
    yield batch


def total_msecs_for_stage(monitoring_infos, prefix):
  """Returns total_msecs for the stage with the given prefix.

  Args:
    monitoring_infos: Beam pipeline MonitoringInfos, usually obtained via
      pipeline.metrics().monitoring_infos().
    prefix: Prefix to match against.

  Returns:
    The sum of the total_msecs counters for all PTRANSFORM stages in the
    monitoring_infos whose labels start with the given prefix.
  """

  total_msecs = 0
  for info in monitoring_infos:
    if info.urn != "beam:metric:ptransform_execution_time:total_msecs:v1":
      continue
    if "PTRANSFORM" not in info.labels:
      continue
    label = info.labels["PTRANSFORM"]
    if label.startswith(prefix):
      total_msecs += info.metric.counter_data.int64_value
  return total_msecs


def report_beam_stages(benchmark_object, name, stages, infos, extras=None):
  """Report the time for the given Beam stages.

  Args:
    benchmark_object: test.Benchmark object (has the report_benchmark method).
    name: Name prefix for the test case. Usually the name of the test case.
    stages: A list of stage names to report the Beam run time for.
    infos: Beam pipeline MonitoringInfos, usually obtained via
      pipeline.metrics().monitoring_infos().
    extras: Extra information to include with the report, will be passed
      directly as the "extras" argument to report_benchmark.
  """
  for stage in stages:
    benchmark_object.report_benchmark(
        name="%s.%s" % (name, stage),
        iters=1,
        wall_time=total_msecs_for_stage(infos, stage) / 1000.0,
        extras=extras)
