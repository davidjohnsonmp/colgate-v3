// Colgate by Hiccup Audio
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The decision stage: how far each band should move, from where it stands in the
// spectrum at this moment.
//
// Each band's level is taken relative to the signal's total level, so absolute
// level plays no part. A small learned field (FieldTuning.h) reads a band's level
// together with its three neighbours on each side and where it sits in the
// spectrum, and gives two numbers, one for Recover and one for Tame. Recover only
// ever acts above its own zero and Tame only below its own, each through a soft knee:
//
//     gain_k = recover * above (fieldR_k) + tame * below (fieldT_k)
//
// One broadband gain then holds the sum over the bands of power^0.35 constant. That
// is why Tame alone lifts the treble and Recover alone dips the mids.
//
// The field, and every number in ModeTuning, come from measuring reference renders
// (see docs/DESIGN.md and tools/calibration). In them 50/50 is the sum of 50/0 and
// 0/50, to 0.13 dB at worst, which is what the sum above and the loudness rule give.
#pragma once

#include "Psychoacoustics.h"

#include <array>

namespace colgate
{

enum class Mode
{
    basic = 0,
    master = 1
};

struct FieldNetwork;

struct ModeTuning
{
    // The learned field (FieldTuning.h), and the value of its mode input.
    const FieldNetwork* field;
    float fieldModeInput;

    // Width of the knee at each control's zero, in dB.
    float softnessDb;

    // Gain follows (amount / 50) ^ amountExponent up to 50%, and grows more slowly
    // above that, by amountExponentHigh.
    float amountExponent, amountExponentHigh;

    // Bias moves the line between the classes by this many dB per unit, and softens
    // the knee on Recover's side by e ^ (biasKnee * Bias), and Tame's the other way.
    float biasLineDb, biasKnee;

    // Brighten: a tilt about 900 Hz, with its treble side leaning over at a knee.
    float tiltLowPerErb, tiltHighPerErb, tiltKneeErb;

    // Negative Brighten is a different shape: it keeps cutting up into the treble,
    // and it barely touches the low side directly.
    float darkenLowPerErb, darkenHighPerErb;

    // Positive Brighten calms the field's movement in the treble.
    float brightenTrebleSlope;

    // A band's energy rises slowly and falls fast.
    float attackSeconds, releaseSeconds;

    // The broadband gains read energy that rises fast and falls more slowly.
    float loudnessAttackSeconds, loudnessReleaseSeconds;

    // Boost: this many times the equal-loudness contour difference, plus a treble
    // correction at Boost +30, at z = 30, 33, 35, 37, 38.5, 40.3. Above 30 dB all of it
    // grows faster than the contours do, by (Boost / 30) ^ boostGrowth.
    float boostScale, boostGrowth;
    std::array<float, 6> boostTrebleDb;
};

const ModeTuning& tuningFor (Mode mode) noexcept;

// Field gain per unit of amount. An amount of 0.5 (50%) is unity.
inline float amountScale (float amount, const ModeTuning& t) noexcept
{
    if (amount <= 0.0f)
        return 0.0f;
    const float ratio = 2.0f * amount;
    return std::pow (ratio, ratio <= 1.0f ? t.amountExponent : t.amountExponentHigh);
}

// The broadband gain in dB that keeps the sum of (band power)^exponent where it
// was. The mask, which may be null, says which bands count. startDb is a first
// guess, so passing the previous answer makes it converge at once.
float keepLoudnessDb (const float* energy, const float* gainDb, const float* mask, int count,
                      float exponent, float startDb) noexcept;

class PerceptualField
{
public:
    void prepare (const BandLayout& layout, double hopSeconds);
    void reset() noexcept;

    // Which bands take part, 0 to 1, from the range limiters. Excluded bands are
    // neither moved nor counted. Allocates nothing, but only call it on a change.
    void setRangeMask (const float* mask) noexcept;

    struct Controls
    {
        float recover = 0.0f;  // 0..2, where 1 is 100%
        float tame = 0.0f;     // 0..2
        float bias = 0.0f;     // -1..1
        float brighten = 0.0f; // -1..1
    };

    // Call every hop, processing or not, with the analyser's raw band power: the
    // field also reads the spectrum over a few longer and shorter times.
    void track (const float* raw) noexcept;

    // energy is smoothed band power. Writes the gain in dB per band.
    void compute (const float* energy, const float* loudnessEnergy, const Controls& controls, Mode mode, float* gainDb) noexcept;

    // How the bands split between the classes at the last compute().
    float recoverShare() const noexcept { return share; }

    // Each control's own part of the last gain, before the broadband gain.
    const float* recoverPart() const noexcept { return recoverGain.data(); }
    const float* tamePart() const noexcept { return tameGain.data(); }

    // Scales everything, by frequency: the top of the spectrum is eased off.
    const float* fade() const noexcept { return topFade.data(); }

    static constexpr float kFloorDb = 60.0f;

private:
    using BandArray = std::array<float, kMaxBands>;
    static constexpr int kMaxHidden = 32;
    static constexpr int kMaxTimeScales = 4;
    static constexpr int kMaxSpreads = 3;
    using HiddenArray = std::array<float, kMaxHidden>;

    int count = 0;
    BandArray z {}, relative {}, mask {}, trebleWeight {}, topFade {};
    BandArray fieldRecover {}, fieldTame {}, slowRecover {}, slowTame {};
    BandArray recoverGain {}, tameGain {}, shape {}, faded {};

    // Band power over the field's other time scales, and the weights that spread
    // power across neighbouring bands.
    std::array<BandArray, kMaxTimeScales> timeScaled {};
    std::array<float, kMaxTimeScales> timeScaleAlpha {};
    std::array<std::array<BandArray, kMaxBands>, kMaxSpreads> spreadKernel {};
    std::array<int, kMaxSpreads> spreadReach {};
    bool tracked = false;

    // What the field's fixed inputs (place in the spectrum, mode) add, per mode and band.
    std::array<std::array<HiddenArray, kMaxBands>, 2> bandHidden {};
    std::array<std::array<std::array<float, 2>, kMaxBands>, 2> bandOutput {};
    float slowAlpha = 0.001f;
    float keepGuess = 0.0f;
    float share = 0.0f;
    bool primed = false;
};

} // namespace colgate
