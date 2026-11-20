# Calibration

How Basic and Master were tuned, so it can be done again with more material. No audio is kept in this repository. Keep it that way, since neither the mix nor the reference renders are ours to publish.

## Make the renders

Bounce each song through the reference plug-in at 48 kHz, once per setting, and keep them in one folder per song. Names and subfolders say what a render is. A name or folder with `master` in it counts as Master mode, and everything else as Basic:

```
ref 1/original.wav
ref 1/basic/recover 50 tame 50.wav
ref 1/basic/recover 15 tame 15 bias 15 brighten 15 boost 30.wav
ref 1/master/tame 50.wav
```

Delay between the files doesn't matter, because it's measured. Leave the plug-in's Gain at 0 dB and everything you aren't testing at its default. Check that the plug-in is really processing during each bounce: a render nearly identical to its original gets reported and left out. Check that each file has sound in it, and that every other control went back to its default before the next one.

Name each render after what was really set, because the name decides what the fit assumes. Two Master renders labelled Boost 30 were really at Boost 50, and that made Master's Boost about twice too strong in version 1.2.

## Train and score

Build the tools (`cmake -S . -B build -DCOLGATE_BUILD_PLUGIN=OFF && cmake --build build`), install numpy and scipy, then:

```sh
# Train, see how closely it matches every render, and write it for the engine
python3 tools/calibration/calibrate.py "ref 1" "ref 2" "ref 3" "ref 4" "ref 5" "ref 6" --build build/tests \
        --write source/engine/FieldTuning.h

# The same, but first score each song with a field trained only on the others (about seven times as long)
python3 tools/calibration/calibrate.py "ref 1" "ref 2" "ref 3" "ref 4" "ref 5" "ref 6" --build build/tests --check

# Rebuild, then compare the engine's actual audio with every render
python3 tools/calibration/calibrate.py "ref 1" "ref 2" "ref 3" "ref 4" "ref 5" "ref 6" --build build/tests --score
```

Training takes about ten minutes on one core for these six songs. It trains a small network, the field, which reads each band's level against its neighbours, over a few time scales, and where it sits in the spectrum, and gives the two numbers that Recover and Tame act on. One field serves both modes, with the mode as one of its inputs, so Basic, with fewer songs, learns from Master's too. Around it sit the parts with a fixed form: the soft knees, how gain grows with amount, Bias, Brighten, Boost and the loudness rule. Their numbers are fitted per mode, alternating with the field, against every render at once, in every band with signal in it. Each render counts the same whatever the size of its gains, except that plain 15/15 and 50/50 count five times (`EQUAL_WEIGHT`): 15/15 is the setting that matters most, and in the reference it is 50/50's shape at a third of the size. Only the part of the gain that the loudness rule doesn't set counts, because the rule sets the rest.

A number that no render measures keeps the value it starts from, which is version 1.4's: Bias with the controls apart, negative Brighten, Boost above 30 and amounts above 50% each need renders of their own. Basic borrows Boost's growth from Master, because none of its renders go above Boost 30.

`--check` is the fair test. A song's scores there come from a field that never heard it, so they say how a new song would do. The scores after training on everything flatter it, because that field has heard every song.

## What more renders would answer

Today's field comes from six songs of 12 to 26 seconds, four of them in Basic. These would help most, in this order:

1. Two files from ref 5 again in Master: Boost 25 came out dry, and Tame 100, Bias 50 looks like a Recover render.
2. Amounts above 50 on other songs, such as 100/100 with no Bias, and 200/200. Only one song has been measured there, and only up to 110.
3. Boost alone at other values, such as +10, -15 and -30. So far: -10, +25, +30 and +50.
4. Bias below zero with Recover and Tame apart, and Tame alone at other Bias values.
5. Negative Brighten on other songs, such as -100, and Brighten +100. Only one song has been measured beyond -16.
6. Basic on the songs that only have Master renders so far.
7. Single sources rather than full mixes, such as one guitar track and one iPhone vocal. Sparse material is the least tested.
