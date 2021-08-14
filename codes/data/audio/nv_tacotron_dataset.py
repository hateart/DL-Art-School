import os
import random

import audio2numpy
import numpy as np
import torch
import torch.utils.data
import torch.nn.functional as F
from tqdm import tqdm

import models.tacotron2.layers as layers
from models.tacotron2.taco_utils import load_wav_to_torch, load_filepaths_and_text

from models.tacotron2.text import text_to_sequence
from utils.util import opt_get


def load_mozilla_cv(filename):
    with open(filename, encoding='utf-8') as f:
        components = [line.strip().split('\t') for line in f][1:]  # First line is the header
        filepaths_and_text = [[f'clips/{component[1]}', component[2]] for component in components]
    return filepaths_and_text


class TextMelLoader(torch.utils.data.Dataset):
    """
        1) loads audio,text pairs
        2) normalizes text and converts them to sequences of one-hot vectors
        3) computes mel-spectrograms from audio files.
    """
    def __init__(self, hparams):
        self.path = os.path.dirname(hparams['path'])
        fetcher_mode = opt_get(hparams, ['fetcher_mode'], 'lj')
        if fetcher_mode == 'lj':
            fetcher_fn = load_filepaths_and_text
        elif fetcher_mode == 'mozilla_cv':
            fetcher_fn = load_mozilla_cv
        else:
            raise NotImplementedError()
        self.audiopaths_and_text = fetcher_fn(hparams['path'])
        self.text_cleaners = hparams.text_cleaners
        self.max_wav_value = hparams.max_wav_value
        self.sampling_rate = hparams.sampling_rate
        self.load_mel_from_disk = hparams.load_mel_from_disk
        self.return_wavs = opt_get(hparams, ['return_wavs'], False)
        self.input_sample_rate = opt_get(hparams, ['input_sample_rate'], self.sampling_rate)
        assert not (self.load_mel_from_disk and self.return_wavs)
        self.stft = layers.TacotronSTFT(
            hparams.filter_length, hparams.hop_length, hparams.win_length,
            hparams.n_mel_channels, hparams.sampling_rate, hparams.mel_fmin,
            hparams.mel_fmax)
        random.seed(hparams.seed)
        random.shuffle(self.audiopaths_and_text)
        self.max_mel_len = opt_get(hparams, ['max_mel_length'], None)
        self.max_text_len = opt_get(hparams, ['max_text_length'], None)
        # If needs_collate=False, all outputs will be aligned and padded at maximum length.
        self.needs_collate = opt_get(hparams, ['needs_collate'], True)
        if not self.needs_collate:
            assert self.max_mel_len is not None and self.max_text_len is not None

    def get_mel_text_pair(self, audiopath_and_text):
        # separate filename and text
        audiopath, text = audiopath_and_text[0], audiopath_and_text[1]
        audiopath = os.path.join(self.path, audiopath)
        text = self.get_text(text)
        mel = self.get_mel(audiopath)
        return (text, mel, audiopath_and_text[0])

    def get_mel(self, filename):
        if not self.load_mel_from_disk:
            if filename.endswith('.wav'):
                audio, sampling_rate = load_wav_to_torch(filename)
                audio = audio / self.max_wav_value
            else:
                audio, sampling_rate = audio2numpy.audio_from_file(filename)
                audio = torch.tensor(audio)

            if sampling_rate != self.input_sample_rate:
                if sampling_rate < self.input_sample_rate:
                    print(f'{filename} has a sample rate of {sampling_rate} which is lower than the requested sample rate of {self.input_sample_rate}. This is not a good idea.')
                audio = torch.nn.functional.interpolate(audio.unsqueeze(0).unsqueeze(1), scale_factor=self.input_sample_rate/sampling_rate, mode='area', recompute_scale_factor=False)
                audio = (audio.squeeze().clip(-1,1)+1)/2
            if (audio.min() < -1).any() or (audio.max() > 1).any():
                print(f"Error with audio ranging for {filename}; min={audio.min()} max={audio.max()}")
                return None
            audio_norm = audio.unsqueeze(0)
            audio_norm = torch.autograd.Variable(audio_norm, requires_grad=False)
            if self.input_sample_rate != self.sampling_rate:
                ratio = self.sampling_rate / self.input_sample_rate
                audio_norm = torch.nn.functional.interpolate(audio_norm.unsqueeze(0), scale_factor=ratio, mode='area').squeeze(0)
            if self.return_wavs:
                melspec = audio_norm
            else:
                melspec = self.stft.mel_spectrogram(audio_norm)
                melspec = torch.squeeze(melspec, 0)
        else:
            melspec = torch.from_numpy(np.load(filename))
            assert melspec.size(0) == self.stft.n_mel_channels, (
                'Mel dimension mismatch: given {}, expected {}'.format(melspec.size(0), self.stft.n_mel_channels))


        return melspec

    def get_text(self, text):
        text_norm = torch.IntTensor(text_to_sequence(text, self.text_cleaners))
        return text_norm

    def __getitem__(self, index):
        t, m, p = self.get_mel_text_pair(self.audiopaths_and_text[index])
        if m is None or \
            (self.max_mel_len is not None and m.shape[-1] > self.max_mel_len) or \
            (self.max_text_len is not None and t.shape[0] > self.max_text_len):
            if m is not None:
                print(f"Exception {index} mel_len:{m.shape[-1]} text_len:{t.shape[0]} fname: {p}")
            # It's hard to handle this situation properly. Best bet is to return the a random valid token and skew the dataset somewhat as a result.
            rv = random.randint(0,len(self)-1)
            return self[rv]
        orig_output = m.shape[-1]
        orig_text_len = t.shape[0]
        if not self.needs_collate:
            if m.shape[-1] != self.max_mel_len:
                m = F.pad(m, (0, self.max_mel_len - m.shape[-1]))
            if t.shape[0] != self.max_text_len:
                t = F.pad(t, (0, self.max_text_len - t.shape[0]))
            return {
                'padded_text': t,
                'input_lengths': torch.tensor(orig_text_len, dtype=torch.long),
                'padded_mel': m,
                'output_lengths': torch.tensor(orig_output, dtype=torch.long),
                'filenames': p
            }
        return t, m, p

    def __len__(self):
        return len(self.audiopaths_and_text)


class TextMelCollate():
    """ Zero-pads model inputs and targets based on number of frames per setep
    """
    def __init__(self, n_frames_per_step):
        self.n_frames_per_step = n_frames_per_step

    def __call__(self, batch):
        """Collate's training batch from normalized text and mel-spectrogram
        PARAMS
        ------
        batch: [text_normalized, mel_normalized, filename]
        """
        # Right zero-pad all one-hot text sequences to max input length
        input_lengths, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([len(x[0]) for x in batch]),
            dim=0, descending=True)
        max_input_len = input_lengths[0]

        text_padded = torch.LongTensor(len(batch), max_input_len)
        text_padded.zero_()
        filenames = []
        for i in range(len(ids_sorted_decreasing)):
            text = batch[ids_sorted_decreasing[i]][0]
            text_padded[i, :text.size(0)] = text
            filenames.append(batch[ids_sorted_decreasing[i]][2])

        # Right zero-pad mel-spec
        num_mels = batch[0][1].size(0)
        max_target_len = max([x[1].size(1) for x in batch])
        if max_target_len % self.n_frames_per_step != 0:
            max_target_len += self.n_frames_per_step - max_target_len % self.n_frames_per_step
            assert max_target_len % self.n_frames_per_step == 0

        # include mel padded and gate padded
        mel_padded = torch.FloatTensor(len(batch), num_mels, max_target_len)
        mel_padded.zero_()
        gate_padded = torch.FloatTensor(len(batch), max_target_len)
        gate_padded.zero_()
        output_lengths = torch.LongTensor(len(batch))
        for i in range(len(ids_sorted_decreasing)):
            mel = batch[ids_sorted_decreasing[i]][1]
            mel_padded[i, :, :mel.size(1)] = mel
            gate_padded[i, mel.size(1)-1:] = 1
            output_lengths[i] = mel.size(1)

        return {
            'padded_text': text_padded,
            'input_lengths': input_lengths,
            'padded_mel': mel_padded,
            'padded_gate': gate_padded,
            'output_lengths': output_lengths,
            'filenames': filenames
        }


def save_mel_buffer_to_file(mel, path):
    np.save(path, mel.numpy())


def load_mel_buffer_from_file(path):
    return torch.tensor(np.load(path))


def dump_mels_to_disk():
    params = {
        'mode': 'nv_tacotron',
        'path': 'E:\\audio\\MozillaCommonVoice\\en\\test.tsv',
        'phase': 'train',
        'n_workers': 0,
        'batch_size': 1,
        'fetcher_mode': 'mozilla_cv',
        'needs_collate': True,
        'max_mel_length': 1000,
        'max_text_length': 200,
        #'return_wavs': True,
        #'input_sample_rate': 22050,
        #'sampling_rate': 8000
    }
    output_path = 'D:\\dlas\\results\\mozcv_mels'
    os.makedirs(os.path.join(output_path, 'clips'), exist_ok=True)
    from data import create_dataset, create_dataloader
    ds, c = create_dataset(params, return_collate=True)
    dl = create_dataloader(ds, params, collate_fn=c)
    for i, b in tqdm(enumerate(dl)):
        mels = b['padded_mel']
        fnames = b['filenames']
        for j, fname in enumerate(fnames):
            save_mel_buffer_to_file(mels[j], f'{os.path.join(output_path, fname)}_mel.npy')


if __name__ == '__main__':
    dump_mels_to_disk()
    '''
    params = {
        'mode': 'nv_tacotron',
        'path': 'E:\\audio\\MozillaCommonVoice\\en\\train.tsv',
        'phase': 'train',
        'n_workers': 12,
        'batch_size': 32,
        'fetcher_mode': 'mozilla_cv',
        'needs_collate': False,
        'max_mel_length': 800,
        'max_text_length': 200,
        #'return_wavs': True,
        #'input_sample_rate': 22050,
        #'sampling_rate': 8000
    }
    from data import create_dataset, create_dataloader

    ds, c = create_dataset(params, return_collate=True)
    dl = create_dataloader(ds, params, collate_fn=c)
    i = 0
    m = None
    for k in range(1000):
        for i, b in tqdm(enumerate(dl)):
            continue
            pm = b['padded_mel']
            pm = torch.nn.functional.pad(pm, (0, 800-pm.shape[-1]))
            m = pm if m is None else torch.cat([m, pm], dim=0)
            print(m.mean(), m.std())
    '''