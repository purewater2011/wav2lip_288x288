from os import listdir, path
import numpy as np
import scipy, cv2, os, sys, argparse, audio
import json, subprocess, random, string
from tqdm import tqdm
from glob import glob
import torch, face_detection
from models import Wav2Lip
import platform
import audio

checkpoint_path = "/mnt2/wav2lip_288x288/checkpoints/llzl2/checkpoint_step000002100.pth"  # 生成器的checkpoint位置
face = "/mnt2/SadTalker/data/girl-word/test_llzl_tiny.mp4"  # 参照视频的文件位置, *.mp4
speech = "/mnt2/SadTalker/data/girl-word/test_girl_tiny.mp3"  # 输入语音的位置，*.wav
resize_factor = 1  # 对输入的视频进行下采样的倍率
crop = [0, -1, 0, -1]  # 是否对视频帧进行裁剪,处理视频中有多张人脸时有用
fps = 25  # 视频的帧率
static = False  # 是否只使用固定的一帧作为视频的生成参照

if not os.path.isfile(face):
    raise ValueError('--face argument must be a valid path to video/image file')


else:  # 若输入的是视频格式
    video_stream = cv2.VideoCapture(face)  # 读取视频
    fps = video_stream.get(cv2.CAP_PROP_FPS)  # 读取 fps

    print('Reading video frames...')

    full_frames = []
    # 提取所有的帧
    while 1:
        still_reading, frame = video_stream.read()
        if not still_reading:
            video_stream.release()
            break
        if resize_factor > 1:  # 进行下采样，降低分辨率
            frame = cv2.resize(frame, (frame.shape[1] // resize_factor, frame.shape[0] // resize_factor))

        y1, y2, x1, x2 = crop  # 裁剪
        if x2 == -1: x2 = frame.shape[1]
        if y2 == -1: y2 = frame.shape[0]

        frame = frame[y1:y2, x1:x2]

        full_frames.append(frame)

print("Number of frames available for inference: " + str(len(full_frames)))

if not os.path.isdir('temp'):
    os.mkdir('temp/')

# 检查输入的音频是否为 .wav格式的，若不是则进行转换
if not speech.endswith('.wav'):
    print('Extracting raw audio...')
    command = 'ffmpeg -y -i {} -strict -2 {}'.format(speech, 'temp/temp.wav')

    subprocess.call(command, shell=True)
    speech = 'temp/temp.wav'

wav = audio.load_wav(speech, 16000)  # 保证采样率为16000
mel = audio.melspectrogram(wav)
print(mel.shape)

wav2lip_batch_size = 128  # 推理时输入到网络的batchsize
mel_step_size = 16

# 提取语音的mel谱
mel_chunks = []
mel_idx_multiplier = 80. / fps
i = 0
while 1:
    start_idx = int(i * mel_idx_multiplier)
    if start_idx + mel_step_size > len(mel[0]):
        mel_chunks.append(mel[:, len(mel[0]) - mel_step_size:])
        break
    mel_chunks.append(mel[:, start_idx: start_idx + mel_step_size])
    i += 1

print("Length of mel chunks: {}".format(len(mel_chunks)))

full_frames = full_frames[:len(mel_chunks)]

batch_size = wav2lip_batch_size

img_size = 96  # 默认的输入图片大小
pads = [0, 20, 0, 0]  # 填充的长度，保证下巴也在抠图的范围之内
nosmooth = False
face_det_batch_size = 16


def get_smoothened_boxes(boxes, T):
    for i in range(len(boxes)):
        if i + T > len(boxes):
            window = boxes[len(boxes) - T:]
        else:
            window = boxes[i: i + T]
        boxes[i] = np.mean(window, axis=0)
    return boxes


# 人脸检测函数
def face_detect(images):
    detector = face_detection.FaceAlignment(face_detection.LandmarksType._2D,
                                            flip_input=False, device=device)

    batch_size = face_det_batch_size

    while 1:
        predictions = []
        try:
            for i in tqdm(range(0, len(images), batch_size)):
                predictions.extend(detector.get_detections_for_batch(np.array(images[i:i + batch_size])))
        except RuntimeError:
            if batch_size == 1:
                raise RuntimeError(
                    'Image too big to run face detection on GPU. Please use the --resize_factor argument')
            batch_size //= 2
            print('Recovering from OOM error; New batch size: {}'.format(batch_size))
            continue
        break

    results = []
    pady1, pady2, padx1, padx2 = pads
    for rect, image in zip(predictions, images):
        if rect is None:
            cv2.imwrite('temp/faulty_frame.jpg', image)  # check this frame where the face was not detected.
            raise ValueError('Face not detected! Ensure the video contains a face in all the frames.')

        y1 = max(0, rect[1] - pady1)
        y2 = min(image.shape[0], rect[3] + pady2)
        x1 = max(0, rect[0] - padx1)
        x2 = min(image.shape[1], rect[2] + padx2)

        results.append([x1, y1, x2, y2])

    boxes = np.array(results)
    if not nosmooth: boxes = get_smoothened_boxes(boxes, T=5)
    results = [[image[y1: y2, x1:x2], (y1, y2, x1, x2)] for image, (x1, y1, x2, y2) in zip(images, boxes)]

    del detector
    return results


box = [-1, -1, -1, -1]


def datagen(frames, mels):
    img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    if box[0] == -1:  # 如果未指定 特定的人脸边界的话
        if not static:  # 是否使用视频的第一帧作为参考
            face_det_results = face_detect(frames)  # BGR2RGB for CNN face detection
        else:
            face_det_results = face_detect([frames[0]])
    else:
        print('Using the specified bounding box instead of face detection...')
        y1, y2, x1, x2 = box
        face_det_results = [[f[y1: y2, x1:x2], (y1, y2, x1, x2)] for f in frames]  # 裁剪出人脸结果

    for i, m in enumerate(mels):
        idx = 0 if static else i % len(frames)
        frame_to_save = frames[idx].copy()
        face, coords = face_det_results[idx].copy()

        face = cv2.resize(face, (img_size, img_size))  # 重采样到指定大小

        img_batch.append(face)
        mel_batch.append(m)
        frame_batch.append(frame_to_save)
        coords_batch.append(coords)

        if len(img_batch) >= wav2lip_batch_size:
            img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

            img_masked = img_batch.copy()
            img_masked[:, img_size // 2:] = 0

            img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
            mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

            yield img_batch, mel_batch, frame_batch, coords_batch
            img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    if len(img_batch) > 0:
        img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

        img_masked = img_batch.copy()
        img_masked[:, img_size // 2:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

        yield img_batch, mel_batch, frame_batch, coords_batch


mel_step_size = 16
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} for inference.'.format(device))


# 加载模型
def _load(checkpoint_path):
    if device == 'cuda':
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint


def load_model(path):
    model = Wav2Lip()
    print("Load checkpoint from: {}".format(path))
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)

    model = model.to(device)
    return model.eval()


full_frames = full_frames[:len(mel_chunks)]

batch_size = wav2lip_batch_size
gen = datagen(full_frames.copy(), mel_chunks)  # 进行人脸的裁剪与拼接，6通道

for i, (img_batch, mel_batch, frames, coords) in enumerate(tqdm(gen,
                                                                total=int(
                                                                    np.ceil(float(len(mel_chunks)) / batch_size)))):
    # 加载模型
    if i == 0:
        model = load_model(checkpoint_path)
        print("Model loaded")

        frame_h, frame_w = full_frames[0].shape[:-1]
        # 暂存临时视频
        out = cv2.VideoWriter('temp/result_without_audio.mp4',
                              cv2.VideoWriter_fourcc(*'DIVX'), fps, (frame_w, frame_h))

    img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
    mel_batch = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(device)

    ##### 将 img_batch, mel_batch送入模型得到pred
    ##############TODO##############
    with torch.no_grad():
        pred = model(mel_batch, img_batch)

    pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.

    for p, f, c in zip(pred, frames, coords):
        y1, y2, x1, x2 = c
        p = cv2.resize(p.astype(np.uint8), (x2 - x1, y2 - y1))

        f[y1:y2, x1:x2] = p
        out.write(f)

out.release()

if not os.path.isdir('results'):
    os.mkdir('results/')

outfile = "results/result.mp4"  # 最终输出结果到该文件夹下
command = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(speech, 'temp/result_without_audio.mp4', outfile)
subprocess.call(command, shell=platform.system() != 'Windows')
