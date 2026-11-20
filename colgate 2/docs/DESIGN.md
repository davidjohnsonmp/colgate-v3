# How Colgate works

Colgate started from three public descriptions of perceptual equalisation: an operation manual, an interview with a developer in the field, and patent application US 2005/0065781. Versions 1.1 to 1.5 were then tuned by measurement against 57 renders of six songs through Gullfoss: all six in its Master mode and four of them in its standard mode. Neither that plug-in's code nor its formulas were available, so this is an independent model fitted to what its output does to the audio. It is close on that material, and it isn't a copy.

Measuring changed the design, sometimes against what the texts suggested.

## What the renders showed

| Measured in the reference | Colgate does | Where |
| --- | --- | --- |
| The EQ is minimum phase. Phase matches the Hilbert transform of log magnitude within about 1 degree. | The log magnitude and its Hilbert transform are built per band and added up. | `ColgateEngine::buildResponseBasis`, `runHop` |
| Both channels get one curve. Gains on left, right, mid and side agree. | One curve from the mean power of both channels. | `ColgateEngine::runAnalysisHop` |
| Recover and Tame add up. On all six pairs measured, 50/50 is the sum of 50/0 and 0/50 to within 0.05 dB RMS, frame by frame. | Each control contributes its own part, and they are summed. | `PerceptualField::compute` |
| Each control both lifts and cuts. Recover alone dips the mids by about 0.7 to 1 dB and Tame alone lifts the treble by about 1.4 to 2 dB. The manual says the same: the combination keeps perceived loudness. | One broadband gain follows the two controls. | `keepLoudnessDb` |
| That broadband gain holds the sum over the bands of power^0.35 constant, with no ear weighting. Frame by frame this stays within about 0.2 dB RMS on three of the four songs with those renders. On the other, 95% of frames are within 0.5 dB, and a few outliers lift its RMS to 1.7 dB. It follows the music over time: correlation about 0.99 for Tame alone and 0.84 to 0.92 for Recover alone. | The same rule, solved every hop. | `keepLoudnessDb` |
| The gain depends on a band's level relative to the total and to its neighbours, in a way no simple formula captured: a flexible function of the same inputs, fitted on some songs, predicted the others better than every formula tried. Levels over a shorter and two longer times help a little more. A masking gradient, tonality, pitch and rhythm layers add under 1%. | A small learned field reads the band's level, three neighbours on each side, the same over 20 ms, 0.3 s and 1.2 s, spread over one and three bands, and where the band sits in the spectrum, with a floor at -60 dB. The masking model and both layers are gone, although the patent describes them. | `PerceptualField::compute`, `FieldTuning.h` |
| Recover and Tame have different zero points across the spectrum. In the bass both act on the same band at once. Tame moves further than Recover for the same change in level, about 1.8 times in Master. | The field gives a separate number for each control, and each acts through a soft knee at its own zero. | `PerceptualField::compute` |
| Bias has no effect with Recover equal to Tame. With them apart it moves the curve modestly, and differently from song to song. | Bias enters only through the difference between the two, so the first is exact. For the second it softens one knee, and in Basic it also moves a line, by amounts fitted per mode, which explains only part of the change. Master's fit has no use for the line. | `PerceptualField::compute` |
| Up to 50%, gain is the same shape at every amount, scaled by (amount / 50)^0.9: 15/15 is 0.337 times 50/50, frame by frame, on every song, with a correlation of 1.00. Above 50% it grows far more slowly, by an exponent of about 0.6. | Those two exponents. The second is measured on one song only, up to 110%. | `amountScale` |
| Brighten is a tilt around 900 Hz that levels off with amount. Upward it levels off quickly in the treble and calms the treble's movement. Downward it's a different shape: the cut keeps deepening up to about 8 kHz, and it barely touches the low side directly. | A separate, nearly straight slope for negative Brighten, in a form that stays smooth through zero. | `PerceptualField::compute` |
| Gains ease off above about 8 kHz, including the broadband gain, however the signal behaves. | The same fade, by frequency. | `kFadeKnotZ` |
| Boost follows the ISO 226 contour difference closely, about 1.2 times as large in both modes at -10, +25 and +30. Only above +30 does it grow faster than the contours do, by (Boost / 30)^0.16. It keeps rising above the standard's 12.5 kHz. | The contour difference, times a scale per mode and that growth, plus a measured treble correction. | `ColgateEngine::updateBoostCurve` |
| Boost holds a different measure: the sum of power^0.5. That reproduces how far the mids drop on each song, within about 0.7 dB at +50 and 0.2 dB at +30. | A second broadband gain for Boost alone. | `ColgateEngine::runAnalysisHop` |
| Energy reacts over 100 to 150 ms. The broadband gains read a faster rise and a slower fall. | Band energy rises with a 120 ms time constant and falls with 40 ms. The broadband gains read a copy that rises with 30 ms and falls with 80 ms, the best of those tried. | `ModeTuning` |
| Master differs mainly in the 3 to 10 kHz area, where Recover lifts about 0.5 to 1 dB less and moves less. Boost is close to the same in both. | A separate set of constants for each mode. | `kBasic`, `kMaster` |

Earlier versions got some of this wrong. Version 1.0 used a masking model and a linear-phase filter. Version 1.1 used a single field and treated Recover and Tame as one line through it, which was fine with them equal and wrong with them apart. Version 1.2 made Master's Boost about twice as strong as it should be, because two renders labelled Boost +30 were really at +50. The renders of songs 4 and 5 showed that, and the earlier two fit +50 far better than +30. Version 1.3 ran too hot above 50%, and its negative Brighten levelled off in the treble, which song 6 showed. Version 1.4 grew gain by an exponent of about 1.1 up to 50%, which made it move too little at 15/15, and its hand-built field missed most at the static shape per band, where songs differ.

## The learned field

Version 1.4's field was a formula: weights for a band's own level and its neighbours', a static curve for each control and two offsets. Measured on songs it hadn't been fitted to, its errors came out like this, as a share of how much the reference's gain varies: 4 to 7% from the static shape per band, which differs from song to song, 2.6% from slow movement, 1.5% from movement between 1 and 5 Hz, 1.1% from faster movement and 1% from the broadband gain. Refitting the formula's parts didn't close that, nor did giving it a look ahead or correcting it per setting. A flexible function of exactly the same inputs did better on songs it hadn't seen, so the information was there and the formula's shape was what fell short.

Version 1.5 replaces the formula with a small network. Per band it reads 14 inputs: the band's level against the whole signal and the levels of three neighbours on each side; the band's level over 20 ms, 0.3 s and 1.2 s, each against its own total; its power spread over one and over three bands; where it sits on the ERB scale; and the mode. Two layers of 16 tanh units and a straight path from the inputs give the two numbers that Recover and Tame act on, from 574 weights, for each of the 50 bands 375 times a second. Everything around it keeps its measured form: the soft knees, the two amount exponents, Bias, Brighten, Boost, the top fade and both loudness rules.

The network and each mode's numbers are trained together on all 57 renders, through every one of those parts, so they are judged on the gain that comes out, in every band with signal in it. Each render counts the same whatever the size of its gains, except that 15/15 and 50/50 with nothing else on count five times: 15/15 is the setting that matters most, and in the reference it is 50/50's shape at a third of the size. Only the part of the gain the broadband rule doesn't set counts, since the rule sets the rest. One network serves both modes, with the mode as an input, so Basic, with four songs, learns from Master's six. It starts as a plain level field and trains in two rounds, alternating with the numbers. `tools/calibration` does all of this and writes `FieldTuning.h`.

## From the sources to the code

| The source says | Colgate does | Where |
| --- | --- | --- |
| Time and frequency relate like position and momentum. A distribution from the phase-space form of quantum theory is used instead of an FFT. | A smoothed pseudo Wigner-Ville distribution, a member of Cohen's class. Publicly known mathematics from Wigner (1932), Moyal (1949) and Cohen (1966). | `PhaseSpaceAnalyzer` |
| Analyse with logarithmic resolution, like the ear (patent [0027]). | Three lag lengths: about 43 ms below 500 Hz, 11 ms up to 3.5 kHz, 3 ms above. | `PhaseSpaceAnalyzer` |
| Bias classifies the range: positive gives Recover more space, negative gives Tame more. | A line moved in the field and a softer knee on one side, acting on the difference between the two controls. | `PerceptualField::compute` |
| Boost is a global parameter that simulates listening level, in dB, and keeps perceived loudness. | Works without Recover or Tame and ignores the range limiters. | `ColgateEngine::updateBoostCurve` |
| Master has fixed best quality and finer control at the same latency. | Master always runs at the Best overlap, shows a decimal more and drags slower unless Shift is down. | `wantedQuality`, `setFineMode` |

## Signal flow

```
                    +-------------------------------------------------+
 main or sidechain  |  phase-space distribution  ->  band energies    |
 ------------------>|  learned field -> Recover part + Tame part      |
                    |  Brighten tilt, broadband gain, top fade         |
                    |  Boost curve and its own broadband gain          |
                    +------------------------+------------------------+
                                             | one set of band gains
                                             v
 main ----> Hann frames ----> minimum-phase filter ----> overlap-add ----> Gain ----> out
   |                                                                    ^
   +-------------------------- delay of the same latency -------------- + crossfade for Bypass
```

The analyser never touches the audio. With every control at zero the engine crossfades to the delay line, which makes that state bit-exact. Latency is one analysis window: 256 samples at 48 kHz, 5.3 ms.

## How close it is

All numbers compare with Gullfoss, through the finished engine's actual audio. The songs are a 26-second clip, two 12-second clips, two 25-second clips and an 18-second clip. Basic has songs 1, 2, 3 and 6. R² is the share of the reference's per-band gain, frame by frame, that Colgate explains, over every band with signal in it, the lowest and highest included. *Curve* is the error of the average EQ curve. *Movement* is the correlation of the gain's movement around that average, and *ratio* is how far it moves against the reference's. *Total* is the RMS gain difference per band and frame, and *dry* is the same for no processing at all. *Null* is the output minus the render, relative to the render minus the dry signal, after correcting any delay of a fraction of a sample between them, so 0 dB would mean the plug-in got nothing right.

**The fair test: songs the field never heard.** Each song is scored with a field and numbers trained only on the other five, and the table averages over songs 1 to 5 and the settings of each kind. The 1.4 column is the same test of version 1.4.

| Setting | Mode | Renders | R² 1.4 | R² 1.5 | Curve | Movement | Ratio | Total | Dry | Null |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 15/15 | Basic | 1 | 0.86 | 0.90 | 0.05 dB | 0.92 | 0.80 | 0.14 dB | 0.49 dB | -7.9 dB |
| 50/50 | Basic | 3 | 0.92 | 0.94 | 0.14 dB | 0.93 | 0.90 | 0.42 dB | 2.14 dB | -11.5 dB |
| Equal, with Bias and Brighten | Basic | 6 | 0.88 | 0.88 | 0.27 dB | 0.88 | 0.96 | 0.34 dB | 1.18 dB | -9.4 dB |
| Recover 50 alone | Basic | 2 | 0.92 | 0.93 | 0.24 dB | 0.88 | 0.85 | 0.41 dB | 1.68 dB | -8.8 dB |
| Tame 50 alone | Basic | 2 | 0.87 | 0.93 | 0.19 dB | 0.93 | 0.91 | 0.29 dB | 1.21 dB | -9.4 dB |
| Boost +30, with 15/15 | Basic | 2 | 0.98 | 0.96 | 0.80 dB | 0.94 | 0.95 | 0.88 dB | 5.13 dB | -15.2 dB |
| 15/15 | Master | 3 | 0.87 | 0.93 | 0.26 dB | 0.83 | 0.77 | 0.17 dB | 0.73 dB | -10.1 dB |
| 50/50 | Master | 5 | 0.91 | 0.94 | 0.26 dB | 0.91 | 0.87 | 0.46 dB | 2.19 dB | -11.2 dB |
| Equal, with Bias and Brighten | Master | 7 | 0.86 | 0.86 | 0.27 dB | 0.84 | 0.95 | 0.31 dB | 0.90 dB | -8.8 dB |
| Recover 50 alone | Master | 4 | 0.91 | 0.93 | 0.28 dB | 0.78 | 0.77 | 0.40 dB | 1.56 dB | -9.0 dB |
| Tame 50 alone | Master | 4 | 0.85 | 0.92 | 0.30 dB | 0.91 | 0.84 | 0.31 dB | 1.26 dB | -9.9 dB |
| Recover 50 alone, Bias +50 | Master | 2 | 0.87 | 0.91 | 0.41 dB | 0.74 | 0.65 | 0.47 dB | 1.65 dB | -9.2 dB |
| Boost +30 or +50, with 15/15 | Master | 4 | 0.97 | 0.96 | 0.78 dB | 0.90 | 0.92 | 1.10 dB | 7.47 dB | -14.0 dB |

**On song 6, the same test.** This song is the only source for amounts above 50%, Bias with the controls apart and negative Brighten at high amounts, so with it left out, the numbers for those come from its renders in the other mode. That's a fair check of those numbers on new renders, but not on a new song.

| Setting | Mode | R² 1.4 | R² 1.5 | Total 1.4 | Total 1.5 | Dry | Null 1.4 | Null 1.5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 16/14, Boost +25 | Basic | 0.94 | 0.97 | 0.86 dB | 0.64 dB | 3.65 dB | -13.4 dB | -18.2 dB |
| 17/15, Bias 34, Brighten -16, Boost -10 | Basic | 0.92 | 0.93 | 0.42 dB | 0.39 dB | 1.62 dB | -15.7 dB | -20.0 dB |
| Boost +25 alone | Basic | 0.96 | 0.97 | 0.69 dB | 0.64 dB | 3.71 dB | -17.4 dB | -19.7 dB |
| Recover 100, Bias 100 | Basic | 0.65 | 0.85 | 1.62 dB | 1.07 dB | 3.18 dB | -5.9 dB | -9.0 dB |
| Tame 100, Bias 50 | Basic | 0.77 | 0.82 | 0.68 dB | 0.60 dB | 1.60 dB | -10.1 dB | -11.2 dB |
| 110/100, Bias 35 | Basic | 0.84 | 0.92 | 1.59 dB | 1.15 dB | 4.70 dB | -8.8 dB | -14.6 dB |
| 110/100, Bias 35, Brighten -40 | Basic | 0.78 | 0.75 | 1.48 dB | 1.58 dB | 3.20 dB | -8.3 dB | -12.1 dB |
| 16/14 | Master | 0.79 | 0.93 | 0.36 dB | 0.21 dB | 0.94 dB | -7.4 dB | -13.4 dB |
| 17/15, Bias 34, Brighten -16, Boost -10 | Master | 0.89 | 0.93 | 0.44 dB | 0.36 dB | 1.50 dB | -15.1 dB | -15.3 dB |
| Recover 100, Bias 100 | Master | 0.87 | 0.84 | 0.82 dB | 0.92 dB | 2.48 dB | -5.5 dB | -10.2 dB |
| 110/100, Bias 35 | Master | 0.90 | 0.94 | 1.17 dB | 0.90 dB | 4.15 dB | -9.8 dB | -14.5 dB |
| 110/100, Bias 35, Brighten -40 | Master | 0.79 | 0.79 | 1.48 dB | 1.48 dB | 3.25 dB | -9.0 dB | -11.9 dB |

Three of this song's Gullfoss renders didn't match their labels. Basic's Recover 16, Tame 14 fits best with Boost +25 still on, so it's used that way. Master's Boost +25 is dry, and Master's Tame 100, Bias 50 looks like a Recover render, so both are left out.

**The whole set.** Above, the songs the field never heard; below, the shipped field, which was trained on every song and so is flattered by it.

| | Renders | R² 1.4 | R² 1.5 | Median 1.5 | At 0.95 or more, 1.4 | At 0.95 or more, 1.5 | Total 1.5 | Null 1.5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Basic, songs it never heard | 23 | 0.881 | 0.907 | 0.924 | 4 | 5 | 0.55 dB | -11.8 dB |
| Master, songs it never heard | 34 | 0.883 | 0.913 | 0.926 | 5 | 5 | 0.50 dB | -10.7 dB |
| Basic, the songs it was trained on | 23 | 0.898 | 0.932 | 0.938 | 6 | 9 | 0.48 dB | -12.6 dB |
| Master, the songs it was trained on | 34 | 0.893 | 0.929 | 0.936 | 5 | 11 | 0.46 dB | -11.5 dB |

On plain 15/15, the setting the training puts first, it scores 0.925 on songs it never heard and 0.943 on the songs it was trained on (1.4: 0.853 and 0.861). Over all 57 renders the shipped field scores 0.930 (1.4: 0.895), with 20 renders at 0.95 or more and 47 at 0.90 or more. On songs it never heard it scores 0.910 (1.4: 0.882), with 10 at 0.95 or more and 38 at 0.90 or more. That second figure is the one to expect on new music, and it doesn't reach 0.95. Of what's left at 15/15, about a third is each song's average EQ shape and two thirds how the gain moves from moment to moment. Six songs don't show enough of either to pin them down: a field half again as large did no better on songs it hadn't heard.

Counting 15/15 and 50/50 five times raised plain 15/15 from 0.905 to 0.925 on songs the field never heard, and 50/50 from 0.935 to 0.944. It cost Brighten +50, which fell below 1.4 on six of its seven renders, and 15/15 with Bias, Brighten or Boost on, which fell by 0.003 on average.

The null's best case is -21.5 dB and its worst -6.5 dB, on song 1 in Master at 15/15 with Bias +15 and Brighten -15.

`tools/calibration` repeats the training and these scores from the audio files. Its training starts from a random field, so a new run comes out close to the shipped one rather than identical.

## What the tests check

All of this runs on every build, using synthetic test material so no audio files are needed: `tests/engine_tests.cpp` and `tests/plugin_tests.cpp`. Values are from x86_64 Linux and may differ slightly on Apple silicon. The test mix has drums, bass, a pad, a voice-like lead and top-end air, with stereo placement.

| Promise | Result |
| --- | --- |
| All controls at zero is no processing | Bit-exact, at 16, 44.1, 48, 96, 192 and 384 kHz, mono and stereo |
| Reported latency is true | The impulse response peaks there, and less than -59 dB of its energy comes earlier, even at the most demanding setting tested |
| EQ is minimum phase | The anti-causal part of its cepstrum is below -120 dB |
| The graph shows the truth | Within 0.01 dB of the measured gain, and within 0.05 dB for the copy the display reads |
| Recover and Tame add up, as in the reference | 50/50 is the sum of 50/0 and 0/50 within 0.04 dB |
| The broadband gain does its job | The sum of power^0.35 holds to 0.17 dB |
| Gain grows with amount as in the reference | 15/15 moves 0.34 to 0.35 times as much as 50/50, against 0.337 in the reference |
| Stems add up to the processed mix (sidechain) | Null at -129 dB |
| Block size doesn't matter | Identical down to 1 sample |
| Both channels get one curve | A level difference between them passes unchanged, to -121 dB |
| Modes share a shape and a latency | Average curves correlate at 0.984, differ by at most 0.9 dB, and latency is equal |
| Switching mode while playing | No click: the largest step stays within 1.0 times the sine's own |
| Bias does nothing with Recover equal to Tame | 0.00 dB, as in the reference. With them apart it moves the curve |
| A masked tone is uncovered | A tone 32 dB under a neighbour 400 Hz below rises about 7.3 dB while the masker falls about 6.3 dB, at 100/100 |
| Boost makes room in the mids | -6.3 dB in Basic and -6.0 dB in Master at Boost +30 on pink noise, and -15.6 dB and -14.9 dB at +50 |
| Pink noise isn't reshaped | Largest band gain 0.5 dB at 100/100, not counting the lowest two bands |
| Entering a number | A double click selects the whole number, so typing replaces it, and Enter sets exactly that. Option-click returns any number or range limiter to its original value |
| Better resolution than a spectrogram | Valley between partials 188 Hz apart: 5.7 dB, against 1.2 dB for a Hann spectrogram of the same span |
| No allocation in the audio path | Counted, including moving every control |
| Bad input is contained | NaN and infinity never reach the output, and audio continues, in both modes |
| Runs in real time | One stereo instance on a 2.1 GHz Xeon cloud core: about 31 times real time at Normal (3% of a core), 20 times at Better, 12 times at Best and in Master (about 9%). The learned field costs about 12 to 15% more than 1.4's formula did on the same core |

The finished VST3 is also loaded through its public interface by a small host (`tools/vst3_host_check.cpp`), which checks parameters, state, audio and the editor.

## Limits worth knowing

- **Six songs is still six songs.** Basic has four, and only three of them with Boost. Leaving one out at a time gives a fair estimate for music like these, and says less about very different styles, levels or arrangements.
- **High amounts rest on one song.** Everything above 50%, most of what's known about Bias with the controls apart, and negative Brighten beyond -16 come from song 6. At 110/100 the highest band lifts about 2 dB too much and the second lowest about 1 to 1.5 dB too little, and Colgate's peak with Brighten -40 is about 2.5 dB over the reference's.
- **Settings nobody measured.** Amounts above 110%. Boost at values other than -10, +25, +30 and +50, anything below -10, and Boost alone in Master. Bias below zero with the controls apart. Brighten beyond +50 or below -40. Those may differ from the reference.
- **Nobody has listened to it.** The match is in numbers, and error can sit in what the numbers miss.
- **It still moves a little less at low settings.** At 15/15 its gain moves 0.68 to 0.85 times as much as the reference, against 0.49 to 0.72 in 1.4, and the average curve is right. This setting is the one most people will use, so it's the first thing to check by ear.
- **Brighten +50 is the weakest of the common settings.** On songs it never heard it scores 0.82 to 0.92 there, below 1.4 on six of its seven renders, the price of putting 15/15 first. Brighten acts through a fixed tilt and a calming of the treble around the field, and the field has no say in it.
- **The field is learned.** It reads the spectrum through a function fitted to six songs rather than a formula, so on material far from them, such as very sparse or very dense mixes or unusual levels, its behaviour is less predictable than 1.4's was.
- **Its movement runs late above the bass.** On nearly every render the gain in the mids and treble moves about 10 to 20 ms after the reference's, while the bass is on time. Shorter time constants close most of that gap, but no score improved when they did, so they stayed.
- **The Mac build gets partial testing.** Version 1.3 in Logic agreed with this Linux build's gain to 0.12 dB RMS at 110/100, on the one render checked that closely. Neither 1.4's nor 1.5's Mac build has been run yet. GitHub Actions builds it on an Intel Mac and runs the tests, auval and pluginval there, so its Apple silicon half is only tested if you start that job by hand.
- **Sparse material.** The field was trained on full mixes. At 15/15 a lone sine stays within 1 dB, but at 100/100 it loses 3.4 to 6.5 dB, partly because the analyser's leakage counts as energy in the loudness rule. At 110 Hz, sidebands sit about 50 dB below the tone at 100/100 and 67 dB below at 15/15. The reference's behaviour on this isn't known. A single guitar or vocal is the next thing worth measuring.
- **Loudness on a meter can move.** The rule that matches the reference isn't BS.1770. On the synthetic mix that means 0.9 LU at 15/15, 2.5 LU at 50/50 and 3.6 LU at 100/100. In its own renders the reference moved up to 1.6 LU at 50/50, and without Boost Colgate tracked it to within 0.4 LU up to 50% and within 0.9 LU above that.
- **Boost's peaks run high.** Colgate's peak is at or above the reference's on all eight renders with positive Boost, by up to 3.2 dB. The worst is song 3 in Master at +50: +1.0 dBFS against -2.2 dBFS. The average gain is close. At the bass hits examined, the reference's broadband gain drops sooner. A broadband gain that falls more slowly narrowed the gap, but slowing it further hurt the match. Watch the output when using Boost. Above 40 dB per band, Boost's curve rises at a quarter of the rate.
- **The deep bass can't be shaped finely.** The filter can't form details narrower than about 45 Hz either side, so below about 100 Hz the EQ acts as a broad move. The graph shows this as it really is.

## Not included

Windows and AAX builds, and the low-latency edition that the manual describes.

## Where to change things

- **Version:** the `project(Colgate VERSION ...)` line in `CMakeLists.txt`.
- **The field and the tuning of each mode:** `source/engine/FieldTuning.h`, which `tools/calibration` writes from the renders. Don't edit it by hand: add renders and train again.
- **Presets:** `kFactoryPresets` in `source/plugin/Parameters.h`.
- **Colours and type:** `source/plugin/Theme.h`.
- **See what the engine does to a signal:** `colgate_measure` and `colgate_render`.
