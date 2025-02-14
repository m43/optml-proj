# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import csv
import inspect
import os
import re

import torch
from torch import optim


def construct_model_name(params, names_params):
    if len(names_params) == 1:
        return names_params[0]
    else:
        params_dict = vars(params)
        outputmodelname = ""
        for n in names_params:
            outputmodelname += str(n) + ":" + str(params_dict[str(n)]) + "-"
        return outputmodelname


def write_results_to_csv(results, outputfile):
    with open(outputfile, 'a', encoding='utf-8') as file:
        # Add header if needed.
        if os.stat(outputfile).st_size == 0:
            line = ''
            for key in results.keys():
                line += key + ';'
            file.write(line[:-1] + '\n')

        results_len = len(results['datapoint_idx'])
        for i in range(results_len):
            line = ''
            for key in results.keys():
                line += str(results[key][i]) + ';'
            file.write(line[:-1] + '\n')


def write_to_csv(scores, params, outputfile):
    """
    This function writes the parameters and the scores with their names in a
    csv file.
    """
    # creates the file if not existing.
    with open(outputfile, 'a', encoding='utf-8') as file:
        # If file is empty writes the keys to the file.
        params_dict = vars(params)
        if os.stat(outputfile).st_size == 0:
            # Writes the configuration parameters
            for key in params_dict.keys():
                file.write(key + ",")
            for i, key in enumerate(scores.keys()):
                ending = "," if i < len(scores.keys()) - 1 else ""
                file.write(key + ending)
            file.write("\n")

    # Writes the values to each corresponding column.
    with open(outputfile, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter=';')
        headers = next(reader)

    # Iterates over the header names and write the corresponding values.
    with open(outputfile, 'a', encoding='utf-8') as f:
        for i, key in enumerate(headers):
            ending = ";" if i < len(headers) - 1 else ""
            if key in params_dict:
                f.write(str(params_dict[key]) + ending)
            elif key in scores:
                f.write(str(scores[key]) + ending)
            else:
                raise AssertionError("Key not found in the given dictionary")
        f.write("\n")


def get_optimizer(s):
    """
    Parse optimizer parameters.
    Input should be of the form:
        - "sgd,lr=0.01"
        - "adagrad,lr=0.1,lr_decay=0.05"
    """
    if "," in s:
        method = s[:s.find(',')]
        optim_params = {}
        for x in s[s.find(',') + 1:].split(','):
            split = x.split('=')
            assert len(split) == 2
            assert re.match("^[+-]?(\d+(\.\d*)?|\.\d+)$", split[1]) is not None
            optim_params[split[0]] = float(split[1])
    else:
        method = s
        optim_params = {}

    if method == 'adadelta':
        optim_fn = optim.Adadelta
    elif method == 'adagrad':
        optim_fn = optim.Adagrad
    elif method == 'adam':
        optim_fn = optim.Adam
    elif method == 'adamax':
        optim_fn = optim.Adamax
    elif method == 'asgd':
        optim_fn = optim.ASGD
    elif method == 'rmsprop':
        optim_fn = optim.RMSprop
    elif method == 'rprop':
        optim_fn = optim.Rprop
    elif method == 'sgd':
        optim_fn = optim.SGD
        assert 'lr' in optim_params
    else:
        raise Exception('Unknown optimization method: "%s"' % method)

    # check that we give good parameters to the optimizer
    expected_args = [key for key in inspect.signature(optim_fn.__init__).parameters.keys()]
    assert expected_args[:2] == ['self', 'params']
    if not all(k in expected_args[2:] for k in [key for key in optim_params.keys()]):
        raise Exception('Unexpected parameters: expected "%s", got "%s"' % (
            str(expected_args[2:]), str(optim_params.keys())))

    return optim_fn, optim_params


"""
Importing batcher and prepare for SentEval
"""


def batcher(batch, params):
    # batch contains list of words
    batch = [['<s>'] + s + ['</s>'] for s in batch]
    sentences = [' '.join(s) for s in batch]
    embeddings = params.infersent.encode(sentences, bsize=params.batch_size,
                                         tokenize=False)

    return embeddings


def prepare(params, samples):
    params.infersent.build_vocab([' '.join(s) for s in samples],
                                 params.glove_path, tokenize=False)


class dotdict(dict):
    """ dot.notation access to dictionary attributes """
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# Multiplies the gradient of the given parameter by a constant.
class GradMulConst(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, const):
        ctx.const = const
        return x.view_as(x)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output * ctx.const, None


def grad_mul_const(x, const):
    return GradMulConst.apply(x, const)
