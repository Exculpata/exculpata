# Synthetic recording fixture

The application fixture at `src/case_intelligence/config/generated-speech.wav` contains only invented software-test speech, synthesized
offline with FFmpeg's [flite source](https://ffmpeg.org/ffmpeg-filters.html#flite),
voice `slt`, then converted to mono 16 kHz PCM. It is not a recording of a person,
client, case or imported record. Tests derive silence, offsets and reduced-volume
variants locally; they do not fetch material at runtime.

Generation command:

```bash
ffmpeg -v error -f lavfi \
  -i 'flite=text=The blue marker is beside the empty box. This is a generated recording for a software test.:voice=slt' \
  -ar 16000 -ac 1 generated-speech.wav
```

This fixture measures plumbing and bounded detector behavior. It is not a
representative speech/language accuracy dataset.
