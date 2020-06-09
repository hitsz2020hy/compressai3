from typing import Union, Tuple

import argparse
import io
import json
import os
import platform
import subprocess
import sys
import time

from tempfile import mkstemp
from collections import defaultdict

import PIL
import PIL.Image as Image

import numpy as np

import torch
from pytorch_msssim import ms_ssim

# from torchvision.datasets.folder
IMG_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.ppm', '.bmp', '.pgm', '.tif',
                  '.tiff', '.webp')


def filesize(filepath: str) -> int:
    """Return file size in bits of `filepath`."""
    if not os.path.isfile(filepath):
        raise ValueError(f'Invalid file "{filepath}".')
    return os.stat(filepath).st_size


def read_image(filepath: str, mode: str = 'RGB') -> np.array:
    """Return PIL image in the specified `mode` format. """
    if not os.path.isfile(filepath):
        raise ValueError(f'Invalid file "{filepath}".')
    return Image.open(filepath).convert(mode)


def compute_metrics(a: Union[np.array, Image.Image],
                    b: Union[np.array, Image.Image],
                    max_val: float = 255.) -> Tuple[float, float]:
    if isinstance(a, Image.Image):
        a = np.asarray(a)
    if isinstance(b, Image.Image):
        b = np.asarray(b)

    a = torch.from_numpy(a.copy()).float().unsqueeze(0)
    if a.size(3) == 3:
        a = a.permute(0, 3, 1, 2)
    b = torch.from_numpy(b.copy()).float().unsqueeze(0)
    if b.size(3) == 3:
        b = b.permute(0, 3, 1, 2)

    mse = torch.mean((a - b)**2).item()
    p = 20 * np.log10(max_val) - 10 * np.log10(mse)
    m = ms_ssim(a, b, data_range=max_val).item()
    return p, m


def rgb2ycbcr(rgb: np.array, bitdepth: int = 8) -> np.array:
    """RGB -> YCbCr, BT.709 conversion."""
    assert bitdepth in (8, 10)
    assert len(rgb.shape) == 3
    assert rgb.shape[0] == 3

    dtype = rgb.dtype
    assert dtype in (np.float32, np.uint8)

    if dtype == np.uint8:
        rgb = rgb.astype(np.float32) / (2**bitdepth - 1)

    ycbcr = np.empty_like(rgb)

    ycbcr[0] = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    ycbcr[1] = (rgb[2] - ycbcr[0]) / 1.8556 + 0.5
    ycbcr[2] = (rgb[0] - ycbcr[0]) / 1.5748 + 0.5

    ycbcr = np.clip(ycbcr, 0, 1)

    if dtype == np.uint8:
        ycbcr = (ycbcr * (2**bitdepth - 1)).astype(np.uint8)

    return ycbcr


def ycbcr2rgb(ycbcr: np.array, bitdepth: int = 8) -> np.array:
    """YCbCr -> RGB, BT.709 conversion."""
    assert bitdepth in (8, 10)
    assert len(ycbcr.shape) == 3
    assert ycbcr.shape[0] == 3

    dtype = ycbcr.dtype
    assert dtype in (np.float32, np.uint8)

    if dtype == np.uint8:
        ycbcr = ycbcr.astype(np.float32) / (2**bitdepth - 1)

    rgb = np.empty_like(ycbcr)
    rgb[0] = 1.5748 * (ycbcr[2] - 0.5) + ycbcr[0]
    rgb[2] = 1.8556 * (ycbcr[1] - 0.5) + ycbcr[0]
    rgb[1] = (ycbcr[0] - 0.2126 * rgb[0] - 0.0722 * rgb[2]) / 0.7152

    rgb = np.clip(rgb, 0, 1)

    return rgb


def run_command(cmd, ignore_returncodes=None):
    cmd = [str(c) for c in cmd]
    try:
        rv = subprocess.check_output(cmd)
        return rv.decode('ascii')
    except subprocess.CalledProcessError as err:
        if ignore_returncodes is not None and \
                err.returncode in ignore_returncodes:
            return err.output
        sys.exit(1)


def _get_ffmpeg_version():
    rv = run_command(['ffmpeg', '-version'])
    return rv.split()[2]


def _get_bpg_version(encoder_path):
    rv = run_command([encoder_path, '-h'], ignore_returncodes=[1])
    return rv.split()[4]


class Codec:
    """Abstract base class"""
    _description = None

    def __init__(self):
        self.qualities = []
        self._parse_args()

    def _get_parser(self):
        description = f'{self.__class__.__name__} codec'
        parser = argparse.ArgumentParser(description=description)
        parser.add_argument('-q',
                            '--quality',
                            metavar='',
                            default=75,
                            nargs='*',
                            type=int,
                            help='quality parameter (default: %(default)s)')
        return parser

    @property
    def description(self):
        return self._description

    @property
    def name(self):
        raise NotImplementedError()

    def _parse_args(self):
        parser = self._get_parser()
        args = parser.parse_args(sys.argv[3:])
        if isinstance(args.quality, list):
            self.qualities = args.quality
        else:
            self.qualities = [args.quality]
        return args

    def _load_img(self, img):
        return os.path.abspath(img)

    def _run(self, img, quality, *args, **kwargs):
        raise NotImplementedError()

    def run(self, img, quality, *args, **kwargs):
        img = self._load_img(img)
        return self._run(img, quality, *args, **kwargs)

    def collect(self, dataset):
        filepaths = [
            os.path.join(dataset, f) for f in os.listdir(dataset)
            if os.path.splitext(f)[-1].lower() in IMG_EXTENSIONS
        ]

        if len(filepaths) == 0:
            print('No images found in the dataset directory')
            sys.exit(1)

        results = defaultdict(list)
        for i, q in enumerate(self.qualities):
            metrics = defaultdict(float)
            for j, f in enumerate(filepaths):
                rv = self.run(f, q)
                for k, v in rv.items():
                    metrics[k] += v
                sys.stderr.write(f'\r{self.name}'
                                 f' | quality: {i+1:d}/{len(self.qualities):d}'
                                 f' | file: {j+1:d}/{len(filepaths):d}')
                sys.stderr.flush()
            for k, v in metrics.items():
                metrics[k] = v / len(filepaths)
                results[k].append(metrics[k])
        sys.stderr.write('\n')
        sys.stderr.flush()
        return results


class PillowCodec(Codec):
    """Abastract codec based on Pillow bindings."""
    fmt = None

    @property
    def name(self):
        raise NotImplementedError()

    def _load_img(self, img):
        return read_image(img)

    def _run(self, img, quality):
        start = time.time()
        tmp = io.BytesIO()
        img.save(tmp, format=self.fmt, quality=int(quality))
        enc_time = time.time() - start
        tmp.seek(0)
        size = tmp.getbuffer().nbytes

        start = time.time()
        rec = Image.open(tmp)
        rec.load()
        dec_time = time.time() - start

        psnr_val, msssim_val = compute_metrics(rec, img)
        bpp_val = float(size) * 8 / (img.size[0] * img.size[1])

        return {
            'psnr': psnr_val,
            'msssim': msssim_val,
            'bpp': bpp_val,
            'encoding_time': enc_time,
            'decoding_time': dec_time,
        }


class JPEG(PillowCodec):
    """Use libjpeg linked in Pillow"""
    fmt = 'jpeg'
    _description = f'JPEG. Pillow version {PIL.__version__}'

    @property
    def name(self):
        return 'JPEG'


class WebP(PillowCodec):
    """Use libwebp linked in Pillow"""
    fmt = 'webp'
    _description = f'WebP. Pillow version {PIL.__version__}'

    @property
    def name(self):
        return 'WebP'


class BinaryCodec(Codec):
    """Call a external binary."""
    fmt = None

    def _run(self, img, quality):
        fd0, png_filepath = mkstemp(suffix='.png')
        fd1, out_filepath = mkstemp(suffix=self.fmt)

        # Encode
        start = time.time()
        run_command(self._get_encode_cmd(img, quality, out_filepath))
        enc_time = time.time() - start
        size = filesize(out_filepath)

        # Decode
        start = time.time()
        run_command(self._get_decode_cmd(out_filepath, png_filepath))
        dec_time = time.time() - start

        # Read image
        img = read_image(img)
        rec = read_image(png_filepath)
        os.close(fd0)
        os.remove(png_filepath)
        os.close(fd1)
        os.remove(out_filepath)

        psnr_val, msssim_val = compute_metrics(rec, img)
        bpp_val = float(size) * 8 / (img.size[0] * img.size[1])

        return {
            'psnr': psnr_val,
            'msssim': msssim_val,
            'bpp': bpp_val,
            'encoding_time': enc_time,
            'decoding_time': dec_time,
        }

    def _get_encode_cmd(self, img, quality, out_filepath):
        raise NotImplementedError()

    def _get_decode_cmd(self, out_filepath, rec_filepath):
        raise NotImplementedError()


class JPEG2000(BinaryCodec):
    """Use ffmpeg version.
    (Not built-in support in default Pillow builds)
    """
    fmt = '.jp2'

    @property
    def name(self):
        return 'JPEG2000'

    @property
    def description(self):
        return f'JPEG2000. ffmpeg version {_get_ffmpeg_version()}'

    def _get_encode_cmd(self, img, quality, out_filepath):
        cmd = [
            'ffmpeg',
            '-loglevel',
            'panic',
            '-y',
            '-i',
            img,
            '-vcodec',
            'jpeg2000',
            '-pix_fmt',
            'yuv420p',
            '-c:v',
            'libopenjpeg',
            '-compression_level',
            quality,
            out_filepath,
        ]
        return cmd

    def _get_decode_cmd(self, out_filepath, rec_filepath):
        cmd = [
            'ffmpeg', '-loglevel', 'panic', '-y', '-i', out_filepath,
            rec_filepath
        ]
        return cmd


class BPG(BinaryCodec):
    """BPG from Fabrice Bellard."""
    fmt = '.bpg'

    @property
    def name(self):
        return f'BPG {self.bitdepth}b {self.subsampling_mode} {self.encoder} {self.color_mode}'

    @property
    def description(self):
        return f'BPG. BPG version {_get_bpg_version(self.encoder_path)}'

    def _get_parser(self):
        parser = super()._get_parser()
        parser.add_argument('-m',
                            choices=['420', '444'],
                            default='444',
                            help='subsampling mode (default: %(default)s)')
        parser.add_argument('-b',
                            choices=['8', '10'],
                            default='8',
                            help='bitdepth (default: %(default)s)')
        parser.add_argument('-c',
                            choices=['rgb', 'ycbcr'],
                            default='ycbcr',
                            help='colorspace  (default: %(default)s)')
        parser.add_argument('-e',
                            choices=['jctvc', 'x265'],
                            default='x265',
                            help='HEVC implementation (default: %(default)s)')
        parser.add_argument('--encoder-path',
                            default='bpgenc',
                            help='BPG encoder path')
        parser.add_argument('--decoder-path',
                            default='bpgdec',
                            help='BPG decoder path')
        return parser

    def _parse_args(self):
        args = super()._parse_args()
        self.color_mode = args.c
        self.encoder = args.e
        self.subsampling_mode = args.m
        self.bitdepth = args.b
        self.encoder_path = args.encoder_path
        self.decoder_path = args.decoder_path
        return args

    def _get_encode_cmd(self, img, quality, out_filepath):
        cmd = [
            self.encoder_path,
            '-o',
            out_filepath,
            '-q',
            str(quality),
            '-f',
            self.subsampling_mode,
            '-e',
            self.encoder,
            '-c',
            self.color_mode,
            '-b',
            self.bitdepth,
            img,
        ]
        return cmd

    def _get_decode_cmd(self, out_filepath, rec_filepath):
        cmd = [self.decoder_path, '-o', rec_filepath, out_filepath]
        return cmd


class TFCI(BinaryCodec):
    """Tensorflow image compression format from tensorflow/comprression"""

    fmt = '.tfci'
    _models = [
        'bmshj2018-factorized-mse',
        'bmshj2018-hyperprior-mse',
        'mbt2018-mean-mse',
    ]

    @property
    def description(self):
        return 'TFCI'

    @property
    def name(self):
        return f'{self.model}'

    def _get_parser(self):
        parser = super()._get_parser()
        parser.add_argument('-m',
                            '--model',
                            choices=self._models,
                            default=self._models[0],
                            help='model architecture (default: %(default)s)')
        parser.add_argument(
            '-p',
            '--path',
            required=True,
            help='tfci python script path (default: %(default)s)')
        return parser

    def _parse_args(self):
        args = super()._parse_args()
        self.model = args.model
        self.tfci_path = args.path
        return args

    def _get_encode_cmd(self, img, quality, out_filepath):
        cmd = [
            sys.executable,
            self.tfci_path,
            'compress',
            f'{self.model}-{quality:d}',
            img,
            out_filepath,
        ]
        return cmd

    def _get_decode_cmd(self, out_filepath, rec_filepath):
        cmd = [
            sys.executable, self.tfci_path, 'decompress', out_filepath,
            rec_filepath
        ]
        return cmd


def get_vtm_encoder_path(build_dir):
    system = platform.system()
    try:
        elfnames = {'Darwin': 'EncoderApp', 'Linux': 'EncoderAppStatic'}
        return os.path.join(build_dir, elfnames[system])
    except KeyError:
        raise RuntimeError(f'Unsupported platform "{system}"')


def get_vtm_decoder_path(build_dir):
    system = platform.system()
    try:
        elfnames = {'Darwin': 'DecoderApp', 'Linux': 'DecoderAppStatic'}
        return os.path.join(build_dir, elfnames[system])
    except KeyError:
        raise RuntimeError(f'Unsupported platform "{system}"')


class VTM(Codec):
    """VTM: VVC reference software"""

    fmt = '.bin'

    @property
    def description(self):
        return 'VTM'

    @property
    def name(self):
        return 'VTM'

    def _get_parser(self):
        parser = super()._get_parser()
        parser.add_argument('-b',
                            '--build-dir',
                            metavar='',
                            type=str,
                            required=True,
                            help='VTM build dir')
        parser.add_argument('-c',
                            '--config',
                            metavar='',
                            type=str,
                            required=True,
                            help='VTM config file')
        parser.add_argument('--rgb',
                            action='store_true',
                            help='Use RGB color space (over YCbCr)')
        return parser

    def _parse_args(self):
        args = super()._parse_args()
        self.encoder_path = get_vtm_encoder_path(args.build_dir)
        self.decoder_path = get_vtm_decoder_path(args.build_dir)
        self.config_path = args.config
        self.rgb = args.rgb
        return args

    def _run(self, img, quality):
        # Convert input image to yuv 444 file
        arr = np.asarray(read_image(img))
        fd, yuv_path = mkstemp(suffix='.yuv')
        out_filepath = os.path.splitext(yuv_path)[0] + '.bin'

        arr = arr.transpose((2, 0, 1))  # color channel first

        if not self.rgb:
            arr = rgb2ycbcr(arr)

        with open(yuv_path, 'wb') as f:
            f.write(arr.tobytes())

        # Encode
        height, width = arr.shape[1:]
        cmd = [
            self.encoder_path,
            '-i',
            yuv_path,
            '-c',
            self.config_path,
            '-q',
            quality,
            '-o',
            '/dev/null',
            '-b',
            out_filepath,
            '-wdt',
            width,
            '-hgt',
            height,
            '-fr',
            '1',
            '-f',
            '1',
            '--InputChromaFormat=444',
            '--InputBitDepth=8',
            '--BDPCM=2',
        ]

        if self.rgb:
            cmd += [
                '--InputColourSpaceConvert=RGBtoGBR',
                '--SNRInternalColourSpace=1',
                '--OutputInternalColourSpace=0',
            ]
        start = time.time()
        run_command(cmd)
        enc_time = time.time() - start

        # cleanup encoder input
        os.close(fd)
        os.unlink(yuv_path)

        # Decode
        cmd = [self.decoder_path, '-b', out_filepath, '-o', yuv_path, '-d', 8]
        if self.rgb:
            cmd.append('--OutputInternalColourSpace=GBRtoRGB')

        start = time.time()
        run_command(cmd)
        dec_time = time.time() - start

        # Compute PSNR
        rec_arr = np.fromfile(yuv_path, dtype=np.uint8)
        rec_arr = rec_arr.reshape(arr.shape)
        bitdepth = 8
        arr = arr.astype(np.float32) / (2**bitdepth - 1)
        rec_arr = rec_arr.astype(np.float32) / (2**bitdepth - 1)
        if not self.rgb:
            arr = ycbcr2rgb(arr)
            rec_arr = ycbcr2rgb(rec_arr)
        psnr_val, msssim_val = compute_metrics(arr, rec_arr, max_val=1.)

        bpp = filesize(out_filepath) * 8. / (height * width)

        # Cleanup
        os.unlink(yuv_path)
        os.unlink(out_filepath)

        return {
            'psnr': psnr_val,
            'msssim': msssim_val,
            'bpp': bpp,
            'encoding_time': enc_time,
            'decoding_time': dec_time
        }


codecs = [JPEG, WebP, JPEG2000, BPG, TFCI, VTM]


def setup_args():
    description = 'Collect codec metrics and performances.'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('codec',
                        type=str,
                        choices=[c.__name__.lower() for c in codecs])
    parser.add_argument('dataset', type=str)
    return parser


def main():
    args = setup_args().parse_args(sys.argv[1:3])

    codec_cls = next(c for c in codecs if c.__name__.lower() == args.codec)
    codec = codec_cls()
    results = codec.collect(args.dataset)

    output = {
        'name': codec.name,
        'description': codec.description,
        'results': results,
    }

    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
