# Copyright 2022, Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Runs HypCluster train and eval experiments."""

import collections
import functools
from typing import Any, Callable, List, OrderedDict, Tuple

from absl import app
from absl import flags
import tensorflow as tf
import tensorflow_federated as tff

from personalization_benchmark.cross_device import constants
from personalization_benchmark.cross_device.algorithms import hypcluster_eval
from personalization_benchmark.cross_device.algorithms import hypcluster_train
from personalization_benchmark.cross_device.algorithms import optimizer_flag_utils
from personalization_benchmark.cross_device.datasets import emnist
from personalization_benchmark.cross_device.datasets import landmark
from personalization_benchmark.cross_device.datasets import stackoverflow
from personalization_benchmark.cross_device.datasets import ted_multi
from utils import training_utils
from utils import utils_impl

with utils_impl.record_hparam_flags() as training_flags:
  # Training loop configuration
  _EXPERIMENT_NAME = flags.DEFINE_string(
      'experiment_name', None, 'The name of this experiment. Will be append to '
      '--root_output_dir to separate experiment results.')
  _DATASET_NAME = flags.DEFINE_enum('dataset_name', None,
                                    constants.DATASET_NAMES,
                                    'Which dataset to use for experiments.')
  _ROOT_OUTPUT_DIR = flags.DEFINE_string(
      'root_output_dir', '/tmp/personalization_benchmark/',
      'Root directory for writing experiment output.')
  _TOTAL_ROUNDS = flags.DEFINE_integer('total_rounds', 100,
                                       'Number of total training rounds.')
  _ROUNDS_PER_EVALUATION = flags.DEFINE_integer(
      'rounds_per_evaluation', 10, 'Frequency of performing evaluation.')
  _ROUNDS_PER_CHECKPOINT = flags.DEFINE_integer(
      'rounds_per_checkpoint', 50, 'Frequency of saving a checkpoint.')

  # Train client configuration
  _CLIENTS_PER_TRAIN_ROUND = flags.DEFINE_integer(
      'clients_per_train_round', 10,
      'How many clients to sample at each training round.')
  _TRAIN_EPOCHS = flags.DEFINE_integer(
      'train_epochs', 1,
      'Number of epochs performed by a client during a round of training.')
  _TRAIN_BATCH_SIZE = flags.DEFINE_integer('train_batch_size', 10,
                                           'Batch size on train clients.')

  # Training and evaluation falgorithm configuration
  _NUM_CLUSTERS = flags.DEFINE_integer(
      'num_clusters', 1, 'Number of clusters used in HypCluster.')
  _PATH_TO_INITIAL_MODEL_WEIGHTS_LIST = flags.DEFINE_list(
      'path_to_initial_model_weights_list', None,
      'Path to load a list of saved Keras model used for initialization. If '
      'None, use random initialization. See `algorithms/checkpoint_utils.py` '
      'for how to extract the model weights from a checkpoint created by our '
      'trainer.')
  _VALID_CLIENTS_PER_EVALUATION = flags.DEFINE_integer(
      'valid_clients_per_evaluation', 100, 'Number of validation clients '
      'sampled to perform finetuning evaluation.')
  _TEST_CLIENTS_PER_EVALUATION = flags.DEFINE_integer(
      'test_clients_per_evaluation', 100, 'Number of test clients sampled to '
      'perform finetuning evaluation.')

  # Random seeds for reproducibility
  _BASE_RANDOM_SEED = flags.DEFINE_integer(
      'base_random_seed', 0, 'An integer random seed governing'
      ' the randomness in the simulation.')

  # Debugging flags
  _USE_SYNTHETIC_DATA = flags.DEFINE_bool(
      'use_synthetic_data', False, 'Whether to use synthetic data. This should '
      'only be set to True for debugging purposes.')

with utils_impl.record_hparam_flags() as optimizer_flags:
  optimizer_flag_utils.define_optimizer_flags('client')
  optimizer_flag_utils.define_optimizer_flags('server')


def _write_hparams():
  """Creates an ordered dictionary of hyperparameter flags and writes to CSV."""
  hparam_dict = utils_impl.lookup_flag_values(training_flags)

  # Update with optimizer flags corresponding to the chosen optimizers.
  opt_flag_dict = utils_impl.lookup_flag_values(optimizer_flags)
  opt_flag_dict = optimizer_flag_utils.remove_unused_flags(
      'client', opt_flag_dict)
  opt_flag_dict = optimizer_flag_utils.remove_unused_flags(
      'server', opt_flag_dict)
  hparam_dict.update(opt_flag_dict)

  # Write the updated hyperparameters to a file.
  training_utils.write_hparams_to_csv(hparam_dict, _ROOT_OUTPUT_DIR.value,
                                      _EXPERIMENT_NAME.value)


def _create_train_algorithm(
    model_fn: Callable[[], tff.learning.Model]
) -> tff.learning.templates.LearningProcess:
  """Creates a learning process for HypCluster training."""
  client_optimizer = optimizer_flag_utils.create_optimizer_from_flags('client')
  server_optimizer = optimizer_flag_utils.create_optimizer_from_flags('server')
  # Need to set `no_nan_division=True` to avoid NaNs in the learned model, which
  # can happen when a model is not selected by any client in a round.
  model_aggregator = tff.aggregators.MeanFactory(no_nan_division=True)
  initial_model_weights_list = None
  if _PATH_TO_INITIAL_MODEL_WEIGHTS_LIST.value is not None:
    initial_model_weights_list = []
    for path_to_saved_model in _PATH_TO_INITIAL_MODEL_WEIGHTS_LIST.value:
      saved_keras_model = tf.keras.models.load_model(path_to_saved_model)
      initial_model_weights_list.append(
          tff.learning.ModelWeights.from_model(saved_keras_model))
  return hypcluster_train.build_hypcluster_train(
      model_fn=model_fn,
      num_clusters=_NUM_CLUSTERS.value,
      client_optimizer=client_optimizer,
      server_optimizer=server_optimizer,
      model_aggregator=model_aggregator,
      initial_model_weights_list=initial_model_weights_list)


def _create_model_and_data(
    dataset_name: str, use_synthetic_data: bool
) -> Tuple[constants.ModelFnType, constants.FederatedDatasetsType,
           constants.ProcessFnType, constants.SplitDataFnType, str]:
  """Creates model, datasets, and processing functions for the given dataset."""
  if dataset_name == 'emnist':
    return emnist.create_model_and_data(
        num_local_epochs=_TRAIN_EPOCHS.value,
        train_batch_size=_TRAIN_BATCH_SIZE.value,
        use_synthetic_data=use_synthetic_data)
  elif dataset_name == 'stackoverflow':
    return stackoverflow.create_model_and_data(
        num_local_epochs=_TRAIN_EPOCHS.value,
        train_batch_size=_TRAIN_BATCH_SIZE.value,
        use_synthetic_data=use_synthetic_data)
  elif dataset_name == 'landmark':
    return landmark.create_model_and_data(
        num_local_epochs=_TRAIN_EPOCHS.value,
        train_batch_size=_TRAIN_BATCH_SIZE.value,
        use_synthetic_data=use_synthetic_data)
  elif dataset_name == 'ted_multi':
    return ted_multi.create_model_and_data(
        num_local_epochs=_TRAIN_EPOCHS.value,
        train_batch_size=_TRAIN_BATCH_SIZE.value,
        use_synthetic_data=use_synthetic_data)
  raise ValueError(f'Accepted dataset names: {constants.DATASET_NAMES}, but '
                   f'found {dataset_name}. Please provide a valid name.')


def _split_data_and_run_hypcluster_eval_computation(
    client_data, model_fn, split_data_fn) -> tff.Computation:
  """Creates a TFF computation to split client data and run hypcluster eval."""

  @tff.tf_computation(tf.string)
  def split_data_for_client(client_id):
    unbatched_data = split_data_fn(client_data.dataset_computation(client_id))
    batched_data = collections.OrderedDict()
    for key in [constants.PERSONALIZATION_DATA_KEY, constants.TEST_DATA_KEY]:
      batched_data[key] = unbatched_data[key].batch(_TRAIN_BATCH_SIZE.value)
    return batched_data

  hypcluster_eval_comp = hypcluster_eval.build_hypcluster_eval_with_dataset_split(
      model_fn=model_fn, num_clusters=_NUM_CLUSTERS.value)
  model_weights_type = hypcluster_eval_comp.type_signature.parameter[0]
  # Note that `tff.simulation.compose_dataset_computation_with_computation` does
  # not work here, because the dataset computation returns a dict of datasets.
  @tff.federated_computation(model_weights_type,
                             tff.types.at_clients(tf.string))
  def split_data_and_run_hypcluster_eval(model_weights_list, client_ids):
    processed_datasets = tff.federated_map(split_data_for_client, client_ids)
    return hypcluster_eval_comp(model_weights_list, processed_datasets)

  return split_data_and_run_hypcluster_eval


def main(argv):
  if len(argv) > 1:
    raise app.UsageError('Expected no command-line arguments, '
                         'got: {}'.format(argv))
  if not _EXPERIMENT_NAME.value:
    raise ValueError('FLAGS.experiment_name must be set.')

  model_fn, federated_datasets, train_preprocess_fn, split_data_fn, _ = (
      _create_model_and_data(_DATASET_NAME.value, _USE_SYNTHETIC_DATA.value))
  train_client_data = federated_datasets[constants.TRAIN_CLIENTS_KEY]
  valid_client_data = federated_datasets[constants.VALID_CLIENTS_KEY]
  test_client_data = federated_datasets[constants.TEST_CLIENTS_KEY]

  # Create the training client selection function, which takes in an integer
  # round number, and outputs a list of client IDs for training in that round.
  training_selection_fn = functools.partial(
      tff.simulation.build_uniform_sampling_fn(
          train_client_data.client_ids, random_seed=_BASE_RANDOM_SEED.value),
      size=_CLIENTS_PER_TRAIN_ROUND.value)

  # Create the training process (and wiring in a dataset computation)
  @tff.tf_computation(tf.string)
  def build_train_dataset_from_client_id(client_id):
    raw_client_data = train_client_data.dataset_computation(client_id)
    return train_preprocess_fn(raw_client_data)

  learning_process = _create_train_algorithm(model_fn)
  training_process = tff.simulation.compose_dataset_computation_with_iterative_process(
      build_train_dataset_from_client_id, learning_process)
  training_process.get_model_weights = learning_process.get_model_weights

  # Create the evaluation client selection function, which takes in an integer
  # round number, and outputs two lists of client IDs: validation clients and
  # test clients. The output of `evaluation_selection_fn` will be used as the
  # second parameter of the `evaluation_fn` below.
  valid_clients_sampling_fn = tff.simulation.build_uniform_sampling_fn(
      valid_client_data.client_ids, random_seed=_BASE_RANDOM_SEED.value)
  test_clients_sampling_fn = tff.simulation.build_uniform_sampling_fn(
      test_client_data.client_ids, random_seed=_BASE_RANDOM_SEED.value)
  evaluation_selection_fn = lambda round_num: (  # pylint: disable=g-long-lambda
      valid_clients_sampling_fn(round_num, _VALID_CLIENTS_PER_EVALUATION.value),
      test_clients_sampling_fn(round_num, _TEST_CLIENTS_PER_EVALUATION.value))

  valid_clients_eval_computation = _split_data_and_run_hypcluster_eval_computation(
      valid_client_data, model_fn, split_data_fn)
  test_clients_eval_computation = _split_data_and_run_hypcluster_eval_computation(
      test_client_data, model_fn, split_data_fn)

  def evaluation_fn(
      state: tff.learning.templates.LearningAlgorithmState,
      sampled_client_ids: Tuple[List[str], List[str]]) -> OrderedDict[str, Any]:
    """Evaluates the current model on the sampled validation and test clients.

    Args:
      state: The current round's state returned by `training_process`.
      sampled_client_ids: A tuple of sample validation and test client ids
        returned by `evaluation_selection_fn`.

    Returns:
      An `OrderedDict` of evaluation metrics on the validation and test clients.
    """
    valid_client_ids, test_client_ids = sampled_client_ids
    raw_valid_metrics = valid_clients_eval_computation(
        training_process.get_model_weights(state), valid_client_ids)
    raw_test_metrics = test_clients_eval_computation(
        training_process.get_model_weights(state), test_client_ids)
    return collections.OrderedDict([
        (constants.VALID_CLIENTS_KEY, raw_valid_metrics),
        (constants.TEST_CLIENTS_KEY, raw_test_metrics)
    ])

  # Configuring release managers and performing training/eval
  program_state_manager, metrics_managers = training_utils.create_managers(
      _ROOT_OUTPUT_DIR.value, _EXPERIMENT_NAME.value)
  _write_hparams()
  tff.simulation.run_training_process(
      training_process=training_process,
      training_selection_fn=training_selection_fn,
      total_rounds=_TOTAL_ROUNDS.value,
      evaluation_fn=evaluation_fn,
      evaluation_selection_fn=evaluation_selection_fn,
      rounds_per_evaluation=_ROUNDS_PER_EVALUATION.value,
      program_state_manager=program_state_manager,
      rounds_per_saving_program_state=_ROUNDS_PER_CHECKPOINT.value,
      metrics_managers=metrics_managers)


if __name__ == '__main__':
  app.run(main)
