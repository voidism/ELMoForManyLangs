#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
import os
import errno
import sys
import codecs
import argparse
import time
import random
import logging
import json
import copy
import tempfile
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from .src.modules.elmo import ElmobiLm
from .src.modules.lstm import LstmbiLm
from .src.modules.token_embedder import ConvTokenEmbedder, LstmTokenEmbedder
from .src.modules.embedding_layer import EmbeddingLayer
from .src.dataloader import load_embedding
import subprocess
import numpy as np
import h5py
import collections

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)-15s %(levelname)s: %(message)s')


def dict2namedtuple(dic):
    return collections.namedtuple('Namespace', dic.keys())(**dic)

def read_list(sents, max_chars=None):
    """
    read raw text file. The format of the input is like, one sentence per line
    words are separated by '\t'

    :param path:
    :param max_chars: int, the number of maximum characters in a word, this
      parameter is used when the model is configured with CNN word encoder.
    :return:
    """
    dataset = []
    textset = []
    for sent in sents:
        data = ['<bos>']
        text = []
        for token in sent:
            text.append(token)
            if max_chars is not None and len(token) + 2 > max_chars:
                token = token[:max_chars - 2]
            data.append(token)
        data.append('<eos>')
        dataset.append(data)
        textset.append(text)
    return dataset, textset


def create_one_batch(x, word2id, char2id, config, oov='<oov>', pad='<pad>', sort=True, use_cuda=False):
    batch_size = len(x)
    lst = list(range(batch_size))
    if sort:
        lst.sort(key=lambda l: -len(x[l]))

    x = [x[i] for i in lst]
    lens = [len(x[i]) for i in lst]
    max_len = max(lens)

    if word2id is not None:
        oov_id, pad_id = word2id.get(oov, None), word2id.get(pad, None)
        assert oov_id is not None and pad_id is not None
        batch_w = torch.LongTensor(batch_size, max_len).fill_(pad_id)
        for i, x_i in enumerate(x):
            for j, x_ij in enumerate(x_i):
                batch_w[i][j] = word2id.get(x_ij, oov_id)
    else:
        batch_w = None

    if char2id is not None:
        bow_id, eow_id, oov_id, pad_id = char2id.get('<eow>', None), char2id.get(
            '<bow>', None), char2id.get(oov, None), char2id.get(pad, None)

        assert bow_id is not None and eow_id != None and oov_id is not None and pad_id is not None

        if config['token_embedder']['name'].lower() == 'cnn':
            max_chars = config['token_embedder']['max_characters_per_token']
            assert max([len(w) for i in lst for w in x[i]]) + 2 <= max_chars
        elif config['token_embedder']['name'].lower() == 'lstm':
            # counting the <bow> and <eow>
            max_chars = max([len(w) for i in lst for w in x[i]]) + 2

        batch_c = torch.LongTensor(
            batch_size, max_len, max_chars).fill_(pad_id)

        for i, x_i in enumerate(x):
            for j, x_ij in enumerate(x_i):
                batch_c[i][j][0] = bow_id
                if x_ij == '<bos>' or x_ij == '<eos>':
                    batch_c[i][j][1] = char2id.get(x_ij)
                    batch_c[i][j][2] = eow_id
                else:
                    for k, c in enumerate(x_ij):
                        batch_c[i][j][k + 1] = char2id.get(c, oov_id)
                    batch_c[i][j][len(x_ij) + 1] = eow_id
    else:
        batch_c = None

    masks = [torch.LongTensor(batch_size, max_len).fill_(0), [], []]

    for i, x_i in enumerate(x):
        for j in range(len(x_i)):
            masks[0][i][j] = 1
            if j + 1 < len(x_i):
                masks[1].append(i * max_len + j)
            if j > 0:
                masks[2].append(i * max_len + j)

    assert len(masks[1]) <= batch_size * max_len
    assert len(masks[2]) <= batch_size * max_len

    masks[1] = torch.LongTensor(masks[1])
    masks[2] = torch.LongTensor(masks[2])

    return batch_w, batch_c, lens, masks

def recover(li, ind):
    # li[piv], ind = torch.sort(li[piv], dim=0, descending=(not unsort))
    dummy = list(range(len(ind)))
    dummy.sort(key=lambda l: ind[l])
    li = [li[i] for i in dummy]
    return li

# shuffle training examples and create mini-batches
def create_batches(x, batch_size, word2id, char2id, config, perm=None, shuffle=False, sort=True, use_cuda=False, text=None):
    ind = list(range(len(x)))
    lst = perm or list(range(len(x)))
    if shuffle:
        random.shuffle(lst)

    if sort:
        lst.sort(key=lambda l: -len(x[l]))

    x = [x[i] for i in lst]
    ind = [ind[i] for i in lst]
    if text is not None:
        text = [text[i] for i in lst]

    sum_len = 0.0
    batches_w, batches_c, batches_lens, batches_masks, batches_text, batches_ind = [], [], [], [], [], []
    size = batch_size
    nbatch = (len(x) - 1) // size + 1
    for i in range(nbatch):
        start_id, end_id = i * size, (i + 1) * size
        bw, bc, blens, bmasks = create_one_batch(x[start_id: end_id], word2id, char2id, config,
                                                 sort=sort, use_cuda=use_cuda)
        sum_len += sum(blens)
        batches_w.append(bw)
        batches_c.append(bc)
        batches_lens.append(blens)
        batches_masks.append(bmasks)
        batches_ind.append(ind[start_id: end_id])
        if text is not None:
            batches_text.append(text[start_id: end_id])

    if sort:
        perm = list(range(nbatch))
        random.shuffle(perm)
        batches_w = [batches_w[i] for i in perm]
        batches_c = [batches_c[i] for i in perm]
        batches_lens = [batches_lens[i] for i in perm]
        batches_masks = [batches_masks[i] for i in perm]
        batches_ind = [batches_ind[i] for i in perm]
        if text is not None:
            batches_text = [batches_text[i] for i in perm]

    logging.info("{} batches, avg len: {:.1f}".format(
        nbatch, sum_len / len(x)))
    recover_ind = [item for sublist in batches_ind for item in sublist]
    if text is not None:
        return batches_w, batches_c, batches_lens, batches_masks, batches_text, recover_ind
    return batches_w, batches_c, batches_lens, batches_masks, recover_ind


class Model(nn.Module):
    def __init__(self, config, word_emb_layer, char_emb_layer, use_cuda=False):
        super(Model, self).__init__()
        self.use_cuda = use_cuda
        self.config = config

        if config['token_embedder']['name'].lower() == 'cnn':
            self.token_embedder = ConvTokenEmbedder(
                config, word_emb_layer, char_emb_layer, use_cuda)
        elif config['token_embedder']['name'].lower() == 'lstm':
            self.token_embedder = LstmTokenEmbedder(
                config, word_emb_layer, char_emb_layer, use_cuda)

        if config['encoder']['name'].lower() == 'elmo':
            self.encoder = ElmobiLm(config, use_cuda)
        elif config['encoder']['name'].lower() == 'lstm':
            self.encoder = LstmbiLm(config, use_cuda)

        self.output_dim = config['encoder']['projection_dim']

    def forward(self, word_inp, chars_package, mask_package):
        token_embedding = self.token_embedder(
            word_inp, chars_package, (mask_package[0].size(0), mask_package[0].size(1)))
        if self.config['encoder']['name'] == 'elmo':
            mask = Variable(mask_package[0]).cuda(
            ) if self.use_cuda else Variable(mask_package[0])
            encoder_output = self.encoder(token_embedding, mask)
            sz = encoder_output.size()
            token_embedding = torch.cat(
                [token_embedding, token_embedding], dim=2).view(1, sz[1], sz[2], sz[3])
            encoder_output = torch.cat(
                [token_embedding, encoder_output], dim=0)
        elif self.config['encoder']['name'] == 'lstm':
            encoder_output = self.encoder(token_embedding)
        return encoder_output

    def load_model(self, path):
        self.token_embedder.load_state_dict(torch.load(os.path.join(path, 'token_embedder.pkl'),
                                                       map_location=lambda storage, loc: storage))
        self.encoder.load_state_dict(torch.load(os.path.join(path, 'encoder.pkl'),
                                                map_location=lambda storage, loc: storage))

class Embedder():
    def __init__(self, model_dir='zht.model/', batch_size=64):
        self.model_dir = model_dir
        self.model, self.config = self.get_model()
        self.batch_size = batch_size
    def get_model(self):
        model_path=os.path.join(os.path.abspath(os.path.join(__file__ ,"..")), self.model_dir)
        # torch.cuda.set_device(1)
        self.use_cuda = torch.cuda.is_available()
        # load the model configurations
        args2 = dict2namedtuple(json.load(codecs.open(
            os.path.join(model_path, 'config.json'), 'r', encoding='utf-8')))

        with open( os.path.join(model_path, args2.config_path), 'r') as fin:
            config = json.load(fin)

        # For the model trained with character-based word encoder.
        if config['token_embedder']['char_dim'] > 0:
            self.char_lexicon = {}
            with codecs.open(os.path.join(model_path, 'char.dic'), 'r', encoding='utf-8') as fpi:
                for line in fpi:
                    tokens = line.strip().split('\t')
                    if len(tokens) == 1:
                        tokens.insert(0, '\u3000')
                    token, i = tokens
                    self.char_lexicon[token] = int(i)
            char_emb_layer = EmbeddingLayer(
                config['token_embedder']['char_dim'], self.char_lexicon, fix_emb=False, embs=None)
            logging.info('char embedding size: ' +
                        str(len(char_emb_layer.word2id)))
        else:
            self.char_lexicon = None
            char_emb_layer = None

        # For the model trained with word form word encoder.
        if config['token_embedder']['word_dim'] > 0:
            self.word_lexicon = {}
            with codecs.open(os.path.join(model_path, 'word.dic'), 'r', encoding='utf-8') as fpi:
                for line in fpi:
                    tokens = line.strip().split('\t')
                    if len(tokens) == 1:
                        tokens.insert(0, '\u3000')
                    token, i = tokens
                    self.word_lexicon[token] = int(i)
            word_emb_layer = EmbeddingLayer(
                config['token_embedder']['word_dim'], self.word_lexicon, fix_emb=False, embs=None)
            logging.info('word embedding size: ' +
                        str(len(word_emb_layer.word2id)))
        else:
            self.word_lexicon = None
            word_emb_layer = None

        # instantiate the model
        model = Model(config, word_emb_layer, char_emb_layer, self.use_cuda)

        if self.use_cuda:
            model.cuda()

        logging.info(str(model))
        model.load_model(model_path)

        # read test data according to input format

        # configure the model to evaluation mode.
        model.eval()
        return model, config

    def sents2elmo(self, sents, output_layer=-1):
        read_function = read_list

        if self.config['token_embedder']['name'].lower() == 'cnn':
            test, text = read_function(sents, self.config['token_embedder']['max_characters_per_token'])
        else:
            test, text = read_function(sents)

        # create test batches from the input data.
        test_w, test_c, test_lens, test_masks, test_text, recover_ind = create_batches(
            test, self.batch_size, self.word_lexicon, self.char_lexicon, self.config, use_cuda=self.use_cuda, text=text)

        cnt = 0

        after_elmo = []
        for w, c, lens, masks, texts in zip(test_w, test_c, test_lens, test_masks, test_text):
            output = self.model.forward(w, c, masks)
            for i, text in enumerate(texts):

                if self.config['encoder']['name'].lower() == 'lstm':
                    data = output[i, 1:lens[i]-1, :].data
                    if self.use_cuda:
                        data = data.cpu()
                    data = data.numpy()
                elif self.config['encoder']['name'].lower() == 'elmo':
                    data = output[:, i, 1:lens[i]-1, :].data
                    if self.use_cuda:
                        data = data.cpu()
                    data = data.numpy()

                if output_layer == -1:
                    payload = np.average(data, axis=0)
                else:
                    payload = data[output_layer]
                after_elmo.append(payload)

                cnt += 1
                if cnt % 1000 == 0:
                    logging.info('Finished {0} sentences.'.format(cnt))

        after_elmo = recover(after_elmo, recover_ind)
        return after_elmo

if __name__ == "__main__":
    # usage example
    e = Embedder()
    w = e.sents2elmo(["今 天 天氣 真 好 阿".split(' '),"潮水 退 了 就 知道".split(' ')])
    print(w[0].shape, w[1].shape)


