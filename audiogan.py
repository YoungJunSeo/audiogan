
import torch as T
import torch.nn as NN
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

import numpy as NP
import numpy.random as RNG
import tensorflow as TF     # for Tensorboard

import argparse
import sys
import datetime
import os

from timer import Timer
import dataset

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as PL

from PIL import Image
import librosa

def tovar(*arrs):
    tensors = [(T.Tensor(a.astype('float32')) if isinstance(a, NP.ndarray) else a).cuda() for a in arrs]
    vars_ = [T.autograd.Variable(t) for t in tensors]
    return vars_[0] if len(vars_) == 1 else vars_


def tonumpy(*vars_):
    arrs = [v.data.cpu().numpy() for v in vars_]
    return arrs[0] if len(arrs) == 1 else arrs


def div_roundup(x, d):
    return (x + d - 1) / d
def roundup(x, d):
    return (x + d - 1) / d * d


def log_sigmoid(x):
    return -F.softplus(-x)
def log_one_minus_sigmoid(x):
    y_neg = T.log(1 - F.sigmoid(x))
    y_pos = -x - T.log(1 + T.exp(-x))
    x_sign = (x > 0).float()
    return x_sign * y_pos + (1 - x_sign) * y_neg


def binary_cross_entropy_with_logits_per_sample(input, target, weight=None):
    if not target.is_same_size(input):
        raise ValueError("Target size ({}) must be the same as input size ({})".format(target.size(), input.size()))

    max_val = (-input).clamp(min=0)
    loss = input - input * target + max_val + ((-max_val).exp() + (-input - max_val).exp()).log()

    if weight is not None:
        loss = loss * weight

    return loss.sum(1)


def advanced_index(t, dim, index):
    return t.transpose(dim, 0)[index].transpose(dim, 0)


def length_mask(size, length):
    batch_size = size[0]
    weight = T.zeros(*size)
    for i in range(batch_size):
        weight[i, :length[i]] = 1.
    weight = tovar(weight)
    return weight


def dynamic_rnn(rnn, seq, length, initial_state):
    length_sorted, length_sorted_idx = T.sort(length, descending=True)
    _, length_inverse_idx = T.sort(length_sorted_idx)
    rnn_in = pack_padded_sequence(
            advanced_index(seq, 1, length_sorted_idx),
            tonumpy(length_sorted),
            )
    rnn_out, rnn_last_state = rnn(rnn_in, initial_state)
    rnn_out = pad_packed_sequence(rnn_out)[0]
    out = advanced_index(rnn_out, 1, length_inverse_idx)
    if isinstance(rnn_last_state, tuple):
        state = tuple(advanced_index(s, 1, length_inverse_idx) for s in rnn_last_state)
    else:
        state = advanced_index(s, 1, length_inverse_idx)

    return out, state


def check_grad(params):
    for p in params:
        if p.grad is None:
            continue
        g = p.grad.data
        anynan = (g != g).long().sum()
        anybig = (g.abs() > 1e+5).long().sum()
        assert anynan == 0
        assert anybig == 0


class Embedder(NN.Module):
    def __init__(self,
                 output_size=100,
                 char_embed_size=50,
                 num_layers=1,
                 num_chars=256,
                 ):
        NN.Module.__init__(self)
        self._output_size = output_size
        self._char_embed_size = char_embed_size
        self._num_layers = num_layers

        self.embed = NN.Embedding(num_chars, char_embed_size)
        self.rnn = NN.LSTM(
                char_embed_size,
                output_size // 2,
                num_layers,
                bidirectional=True,
                )

    def forward(self, chars, length):
        num_layers = self._num_layers
        batch_size = chars.size()[0]
        output_size = self._output_size

        embed_seq = self.embed(chars).permute(1, 0, 2)
        initial_state = (
                tovar(T.zeros(num_layers * 2, batch_size, output_size // 2)),
                tovar(T.zeros(num_layers * 2, batch_size, output_size // 2)),
                )
        embed, (h, c) = dynamic_rnn(self.rnn, embed_seq, length, initial_state)
        h = h.permute(1, 0, 2)
        return h[:, -2:].view(batch_size, output_size)


class Generator(NN.Module):
    def __init__(self,
                 frame_size=200,
                 embed_size=200,
                 noise_size=100,
                 state_size=1024,
                 num_layers=1,
                 ):
        NN.Module.__init__(self)
        self._frame_size = frame_size
        self._noise_size = noise_size
        self._state_size = state_size
        self._embed_size = embed_size
        self._num_layers = num_layers

        self.rnn = NN.ModuleList()
        self.rnn.append(NN.LSTMCell(frame_size + embed_size + noise_size, state_size))
        for _ in range(1, num_layers):
            self.rnn.append(NN.LSTMCell(state_size, state_size))
        self.proj = NN.Linear(state_size, frame_size)
        self.stopper = NN.Linear(state_size, 1)

    def forward(self, batch_size=None, length=None, z=None, c=None):
        frame_size = self._frame_size
        noise_size = self._noise_size
        state_size = self._state_size
        embed_size = self._embed_size
        num_layers = self._num_layers

        if z is None:
            nframes = div_roundup(length, frame_size)
            z = tovar(T.randn(batch_size, nframes, noise_size))
        else:
            batch_size, nframes, _ = z.size()

        c = c.unsqueeze(1).expand(batch_size, nframes, embed_size)
        z = T.cat([z, c], 2)

        lstm_h = [tovar(T.zeros(batch_size, state_size)) for _ in range(num_layers)]
        lstm_c = [tovar(T.zeros(batch_size, state_size)) for _ in range(num_layers)]
        x_t = tovar(T.zeros(batch_size, frame_size))
        generating = T.ones(batch_size).long()
        length = T.zeros(batch_size).long()

        x_list = []
        s_list = []
        stop_list = []
        for t in range(nframes):
            z_t = z[:, t]
            _x = T.cat([x_t, z_t], 1)
            lstm_h[0], lstm_c[0] = self.rnn[0](_x, (lstm_h[0], lstm_c[0]))
            for i in range(1, num_layers):
                lstm_h[i], lstm_c[i] = self.rnn[i](lstm_h[i-1], (lstm_h[i], lstm_c[i]))
            x_t = self.proj(lstm_h[-1]).tanh_()
            logit_s_t = self.stopper(lstm_h[-1])
            s_t = log_sigmoid(logit_s_t)
            s1_t = log_one_minus_sigmoid(logit_s_t)

            logp_t = T.cat([s1_t, s_t], 1)
            p_t = logp_t.exp()
            stop_t = p_t.multinomial()
            length += generating

            x_list.append(x_t)
            s_list.append(logit_s_t.squeeze())
            stop_list.append(stop_t)

            stop_t = stop_t.squeeze()
            generating *= (stop_t.data == 0).long().cpu()
            if generating.sum() == 0:
                break

        x = T.cat(x_list, 1)
        s = T.stack(s_list, 1)

        return x, s, stop_list, tovar(length * frame_size)


class Discriminator(NN.Module):
    def __init__(self,
                 frame_size=200,
                 state_size=1024,
                 embed_size=200,
                 num_layers=1):
        NN.Module.__init__(self)
        self._frame_size = frame_size
        self._state_size = state_size
        self._embed_size = embed_size
        self._num_layers = num_layers

        self.rnn = NN.LSTM(
                frame_size + embed_size,
                state_size // 2,
                num_layers,
                bidirectional=True,
                )
        self.classifier = NN.Sequential(
                NN.Linear(state_size, state_size // 2),
                NN.LeakyReLU(),
                NN.Linear(state_size // 2, 1),
                )

    def forward(self, x, length, c):
        frame_size = self._frame_size
        state_size = self._state_size
        num_layers = self._num_layers
        embed_size = self._embed_size
        batch_size, maxlen = x.size()
        max_nframes = div_roundup(maxlen, frame_size)
        nframes = div_roundup(length, frame_size)

        x = x.view(batch_size, max_nframes, frame_size)
        c = c.unsqueeze(1).expand(batch_size, max_nframes, embed_size)
        x = T.cat([x, c], 2).permute(1, 0, 2)

        initial_state = (
                tovar(T.zeros(num_layers * 2, batch_size, state_size // 2)),
                tovar(T.zeros(num_layers * 2, batch_size, state_size // 2)),
                )

        lstm_out, (lstm_h, lstm_c) = dynamic_rnn(self.rnn, x, nframes, initial_state)
        lstm_out = lstm_out.permute(1, 0, 2)
        max_nframes = lstm_out.size()[1]

        classifier_in = lstm_out.view(batch_size * max_nframes, state_size)
        classifier_out = self.classifier(classifier_in).view(batch_size, max_nframes)

        return classifier_out


parser = argparse.ArgumentParser()
parser.add_argument('--critic_iter', default=5, type=int)
parser.add_argument('--rnng_layers', type=int, default=1)
parser.add_argument('--rnnd_layers', type=int, default=1)
parser.add_argument('--framesize', type=int, default=200, help='# of amplitudes to generate at a time for RNN')
parser.add_argument('--noisesize', type=int, default=100, help='noise vector size')
parser.add_argument('--gstatesize', type=int, default=1024, help='RNN state size')
parser.add_argument('--dstatesize', type=int, default=1024, help='RNN state size')
parser.add_argument('--batchsize', type=int, default=32)
parser.add_argument('--dgradclip', type=float, default=0.0)
parser.add_argument('--ggradclip', type=float, default=0.0)
parser.add_argument('--dlr', type=float, default=1e-4)
parser.add_argument('--glr', type=float, default=1e-5)
parser.add_argument('--modelname', type=str, default = '')
parser.add_argument('--modelnamesave', type=str, default='')
parser.add_argument('--modelnameload', type=str, default='')
parser.add_argument('--just_run', type=str, default='')
parser.add_argument('--loaditerations', type=int, default=0)
parser.add_argument('--logdir', type=str, default='.', help='log directory')
parser.add_argument('--dataset', type=str, default='dataset.h5')
parser.add_argument('--embedsize', type=int, default=100)
parser.add_argument('--minwordlen', type=int, default=1)
parser.add_argument('--maxlen', type=int, default=0)

args = parser.parse_args()
args.conditional = True
png_file = 'temp%s.png' % args.modelname
wav_file = 'temp%s.wav' % args.modelname

if args.just_run not in ['', 'gen', 'dis']:
    print('just run should be empty string, gen, or dis. Other values not accepted')
    sys.exit(0)

if len(args.modelname) > 0:
    modelnamesave = args.modelname
    modelnameload = None
else:
    modelnamesave = args.modelnamesave
    modelnameload = args.modelnameload

print modelnamesave
print args

batch_size = args.batchsize

dataset_h5, maxlen, dataloader, dataloader_val = dataset.dataloader(batch_size, args, maxlen=args.maxlen, frame_size=args.framesize)

def logdirs(logdir, modelnamesave):
    logdir = (
            logdir + '/%s-%s' % 
            (modelnamesave, datetime.datetime.strftime(
                datetime.datetime.now(), '%Y%m%d%H%M%S')
                )
            )
    if not os.path.exists(logdir):
        os.mkdir(logdir)
    elif not os.path.isdir(logdir):
        raise IOError('%s is not a directory' % logdir)
    return logdir
log_train_d = logdirs(args.logdir, modelnamesave)

g = Generator(
        frame_size=args.framesize,
        noise_size=args.noisesize,
        state_size=args.gstatesize,
        embed_size=args.embedsize,
        num_layers=args.rnng_layers,
        ).cuda()
nframes = div_roundup(maxlen, args.framesize)
z_fixed = tovar(RNG.randn(batch_size, nframes, args.noisesize))

e_g = Embedder(args.embedsize).cuda()
e_d = Embedder(args.embedsize).cuda()
cseq, cseq_fixed, clen_fixed = dataset.pick_words(batch_size, dataset_h5, args)
cseq_fixed, clen_fixed = tovar(cseq_fixed, clen_fixed)
cseq_fixed = cseq_fixed.long()
clen_fixed = clen_fixed.long()

d = Discriminator(
        frame_size=args.framesize,
        state_size=args.dstatesize,
        embed_size=args.embedsize,
        num_layers=args.rnnd_layers,
        ).cuda()

def add_waveform_summary(writer, word, sample, gen_iter, tag='plot'):
    PL.plot(sample)
    PL.savefig(png_file)
    PL.close()
    with open(png_file, 'rb') as f:
        imgbuf = f.read()
    img = Image.open(png_file)
    summary = TF.Summary.Image(
            height=img.height,
            width=img.width,
            colorspace=3,
            encoded_image_string=imgbuf
            )
    summary = TF.Summary.Value(tag='%s/%s' % (tag, word), image=summary)
    writer.add_summary(TF.Summary(value=[summary]), gen_iter)

def add_audio_summary(writer, word, sample, length, gen_iter, tag='audio'):
    librosa.output.write_wav(wav_file, sample, sr=8000)
    with open(wav_file, 'rb') as f:
        wavbuf = f.read()
    summary = TF.Summary.Audio(
            sample_rate=8000,
            num_channels=1,
            length_frames=length,
            encoded_audio_string=wavbuf,
            content_type='audio/wav'
            )
    summary = TF.Summary.Value(tag='%s/%s' % (tag, word), audio=summary)
    d_train_writer.add_summary(TF.Summary(value=[summary]), gen_iter)

d_train_writer = TF.summary.FileWriter(log_train_d)

# Add real waveforms
for i in range(batch_size):
    sample, length = dataset.pick_sample_from_word(cseq[i], maxlen, dataset_h5, args.framesize)
    add_waveform_summary(d_train_writer, cseq[i], sample[:length], 0, 'real_plot')
    add_audio_summary(d_train_writer, cseq[i], sample[:length], length, 0, 'real_audio')

gen_iter = 0
epoch = 1
l = 10
alpha = 0.1
baseline = 0.

param_g = list(g.parameters()) + list(e_g.parameters())
param_d = list(d.parameters()) + list(e_d.parameters())

opt_g = T.optim.RMSprop(param_g, lr=args.glr)
opt_d = T.optim.SGD(param_d, lr=args.dlr)
if __name__ == '__main__':
    if modelnameload:
        if len(modelnameload) > 0:
            d = T.load('%s-dis-%05d' % (modelnameload, args.loaditerations))
            g = T.load('%s-gen-%05d' % (modelnameload, args.loaditerations))
            e_g = T.load('%s-eg-%05d' % (modelnameload, args.loaditerations))
            e_d = T.load('%s-ed-%05d' % (modelnameload, args.loaditerations))

    while True:
        _epoch = epoch

        for p in param_g:
            p.requires_grad = False
        for j in range(args.critic_iter):
            with Timer.new('load', print_=False):
                epoch, batch_id, real_data, real_len, _, cs, cl, _, csw, clw = dataloader.next()
                _, cs2, cl2 = dataset.pick_words(batch_size, dataset_h5, args)

            with Timer.new('train_d', print_=False):
                real_data = tovar(real_data + RNG.randn(*real_data.shape))
                real_len = tovar(real_len).long()
                cs = tovar(cs).long()
                cl = tovar(cl).long()

                embed_d = e_d(cs, cl)
                cls_d = d(real_data, real_len, embed_d)
                target = tovar(T.ones(*(cls_d.size())))
                weight = length_mask(cls_d.size(), div_roundup(real_len.data, args.framesize))
                loss_d = F.binary_cross_entropy_with_logits(cls_d, target, weight=weight, size_average=False) / batch_size

                cs2 = tovar(cs2).long()
                cl2 = tovar(cl2).long()
                embed_g = e_g(cs2, cl2)
                embed_d = e_d(cs2, cl2)
                fake_data, _, _, fake_len = g(batch_size=batch_size, length=maxlen, c=embed_g)
                cls_g = d(fake_data + tovar(T.randn(*fake_data.size())), fake_len, embed_d)
                target = tovar(T.zeros(*(cls_g.size())))
                weight = length_mask(cls_g.size(), div_roundup(fake_len.data, args.framesize))
                loss_g = F.binary_cross_entropy_with_logits(cls_g, target, weight=weight, size_average=False) / batch_size
                loss = loss_d + loss_g

                opt_d.zero_grad()
                loss.backward()
                check_grad(param_d)
                opt_d.step()

            loss_d, loss_g, loss, cls_d, cls_g = tonumpy(loss_d, loss_g, loss, cls_d, cls_g)
            d_train_writer.add_summary(
                    TF.Summary(
                        value=[
                            TF.Summary.Value(tag='loss_d', simple_value=loss_d),
                            TF.Summary.Value(tag='loss_g', simple_value=loss_g),
                            TF.Summary.Value(tag='loss', simple_value=loss),
                            TF.Summary.Value(tag='cls_d/mean', simple_value=cls_d.mean()),
                            TF.Summary.Value(tag='cls_d/std', simple_value=cls_d.std()),
                            TF.Summary.Value(tag='cls_g/mean', simple_value=cls_g.mean()),
                            TF.Summary.Value(tag='cls_g/std', simple_value=cls_g.std()),
                            ]
                        ),
                    gen_iter * args.critic_iter + j
                    )

            print 'D', epoch, batch_id, loss, Timer.get('load'), Timer.get('train_d')

        gen_iter += 1
        for p in param_g:
            p.requires_grad = True

        _, cs, cl = dataset.pick_words(batch_size, dataset_h5, args)
        with Timer.new('train_g', print_=False):
            cs = tovar(cs).long()
            cl = tovar(cl).long()
            embed_g = e_g(cs, cl)
            embed_d = e_d(cs, cl)
            fake_data, fake_s, fake_stop_list, fake_len = g(batch_size=batch_size, length=maxlen, c=embed_g)
            fake_data += tovar(T.randn(*fake_data.size()))
            cls_g = d(fake_data, fake_len, embed_d)
            target = tovar(T.ones(*(cls_g.size())))
            weight = length_mask(cls_g.size(), div_roundup(fake_len.data, args.framesize))
            loss = binary_cross_entropy_with_logits_per_sample(cls_g, target, weight=weight)

            reward = -loss.data
            baseline = baseline * 0.999 + reward.mean() * 0.001
            d_train_writer.add_summary(
                    TF.Summary(
                        value=[
                            TF.Summary.Value(tag='reward_baseline', simple_value=baseline),
                            TF.Summary.Value(tag='reward/mean', simple_value=reward.cpu().numpy().mean()),
                            TF.Summary.Value(tag='reward/std', simple_value=reward.cpu().numpy().std()),
                            ]
                        ),
                    gen_iter
                    )
            reward = (reward - baseline).unsqueeze(1) * weight.data
            loss = loss.mean()
            for i, fake_stop in enumerate(fake_stop_list):
                fake_stop.reinforce(reward[:, i:i+1])
            opt_g.zero_grad()
            loss.backward(retain_graph=True)
            T.autograd.backward(fake_stop_list, [None for _ in fake_stop_list])
            opt_g.step()

        if gen_iter % 10 == 0:
            embed_g = e_g(cseq_fixed, clen_fixed)
            fake_data, _, _, fake_len = g(z=z_fixed, c=embed_g)
            fake_data, fake_len = tonumpy(fake_data, fake_len)

            for batch in range(batch_size):
                fake_sample = fake_data[batch, :fake_len[batch]]
                add_waveform_summary(d_train_writer, cseq[batch], fake_sample, gen_iter)

            if gen_iter % 50 == 0:
                for batch in range(batch_size):
                    fake_sample = fake_data[batch, :fake_len[batch]]
                    add_audio_summary(d_train_writer, cseq[batch], fake_sample, fake_len[batch], gen_iter)
                T.save(d, '%s-dis-%05d' % (modelnamesave, gen_iter + args.loaditerations))
                T.save(g, '%s-gen-%05d' % (modelnamesave, gen_iter + args.loaditerations))
                T.save(e_g, '%s-eg-%05d' % (modelnamesave, gen_iter + args.loaditerations))
                T.save(e_d, '%s-ed-%05d' % (modelnamesave, gen_iter + args.loaditerations))

        print 'G', gen_iter, tonumpy(loss), Timer.get('train_g')
