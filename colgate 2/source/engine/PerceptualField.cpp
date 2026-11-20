// Colgate by Hiccup Audio
// SPDX-License-Identifier: AGPL-3.0-or-later
#include "PerceptualField.h"
#include "FieldTuning.h"

namespace colgate
{

namespace
{
// Both modes' field and numbers, measured against reference renders: four songs in
// Basic, six in Master. See docs/DESIGN.md, and tools/calibration, which writes them.
constexpr const ModeTuning& kBasic = fieldtuning::kBasic;
constexpr const ModeTuning& kMaster = fieldtuning::kMaster;

constexpr float kFadeKnotZ[7] = { 28.0f, 33.0f, 36.0f, 38.0f, 39.5f, 40.5f, 41.3f };
constexpr float kFadeKnotValue[7] = { 1.0f, 1.0f, 0.95f, 0.85f, 0.62f, 0.2f, 0.0f };

constexpr float kPivotZ = 14.83f;        // 900 Hz
constexpr float kDarkenKneeErb = 40.0f;  // negative Brighten, close to a straight line
constexpr float kBrightenBend = 0.25f;   // how soon Brighten's tilt levels off
constexpr float kFieldLoudnessExponent = 0.35f;
constexpr float kLoudnessFloor = 7.94e-11f;
constexpr float kMaxKeepDb = 24.0f;
constexpr float kGainLimitDb = 30.0f;
constexpr double kSlowFieldSeconds = 2.0;
constexpr int kFieldReach = 3;          // neighbours the field reads on each side

template <int N>
float interpolate (const float (&x)[N], const float* y, float at) noexcept
{
    if (at <= x[0])
        return y[0];
    for (int i = 0; i + 1 < N; ++i)
        if (at <= x[i + 1])
            return y[i] + (at - x[i]) / (x[i + 1] - x[i]) * (y[i + 1] - y[i]);
    return y[N - 1];
}

inline float above (float x, float softnessSquared) noexcept
{
    return 0.5f * (x + std::sqrt (x * x + softnessSquared));
}

inline float below (float x, float softnessSquared) noexcept
{
    return 0.5f * (x - std::sqrt (x * x + softnessSquared));
}

// tanh to within 1e-4, which is far finer than the field needs.
inline float fieldTanh (float x) noexcept
{
    x = std::max (-4.97f, std::min (4.97f, x));
    const float x2 = x * x;
    return x * (135135.0f + x2 * (17325.0f + x2 * (378.0f + x2)))
         / (135135.0f + x2 * (62370.0f + x2 * (3150.0f + 28.0f * x2)));
}

inline float levelInput (float relativeDb) noexcept { return (relativeDb + 25.0f) / 12.0f; }
inline float placeInput (float erbRate) noexcept { return (erbRate - 20.0f) / 12.0f; }

constexpr int kLevelInputs = 2 * kFieldReach + 1;
constexpr int kPlaceInput = kLevelInputs;
constexpr int kModeInput = kPlaceInput + 1;
constexpr int kExtraInputs = kModeInput + (FieldNetwork::modeInput ? 1 : 0);
static_assert (FieldNetwork::inputs == kExtraInputs + FieldNetwork::timeScales + FieldNetwork::spreads,
               "FieldTuning.h doesn't match the inputs PerceptualField gives the field");
} // namespace

const ModeTuning& tuningFor (Mode mode) noexcept
{
    return mode == Mode::master ? kMaster : kBasic;
}

float keepLoudnessDb (const float* energy, const float* gainDb, const float* mask, int count,
                      float exponent, float startDb) noexcept
{
    const float floorPowered = std::pow (kLoudnessFloor, exponent);

    float wanted = 0.0f;
    std::array<float, kMaxBands> gained;
    for (int k = 0; k < count; ++k)
    {
        const float e = mask != nullptr ? mask[k] * energy[k] : energy[k];
        wanted += std::pow (e + kLoudnessFloor, exponent) - floorPowered;
        gained[(size_t) k] = e * dbToPow (gainDb[k]);
    }
    if (wanted < 1.0e-5f)
        return 0.0f;

    float db = startDb;
    for (int iteration = 0; iteration < 4; ++iteration)
    {
        const float scale = dbToPow (db);
        float value = 0.0f, slope = 0.0f;
        for (int k = 0; k < count; ++k)
        {
            const float e = scale * gained[(size_t) k];
            const float total = e + kLoudnessFloor;
            const float powered = std::pow (total, exponent);
            value += powered - floorPowered;
            slope += exponent * powered * (e / total);
        }
        if (slope < 1.0e-9f)
            return 0.0f;
        const float step = (wanted - value) / (slope * 0.2302585093f);
        db = std::max (-kMaxKeepDb, std::min (kMaxKeepDb, db + std::max (-8.0f, std::min (8.0f, step))));
        if (std::abs (step) < 0.002f)
            break;
    }
    return db;
}

void PerceptualField::prepare (const BandLayout& layout, double hopSeconds)
{
    count = layout.count;
    slowAlpha = (float) (1.0 - std::exp (-hopSeconds / kSlowFieldSeconds));
    reset();

    for (int k = 0; k < count; ++k)
    {
        z[(size_t) k] = layout.z[(size_t) k];
        mask[(size_t) k] = 1.0f;
        const float t = std::max (0.0f, std::min (1.0f, (z[(size_t) k] - 23.0f) / 7.0f));
        trebleWeight[(size_t) k] = t * t * (3.0f - 2.0f * t);
        topFade[(size_t) k] = interpolate (kFadeKnotZ, kFadeKnotValue, z[(size_t) k]);
    }

    static_assert (FieldNetwork::hidden <= kMaxHidden, "raise kMaxHidden");
    static_assert (FieldNetwork::timeScales <= kMaxTimeScales && FieldNetwork::spreads <= kMaxSpreads, "raise the maximums");
    for (int i = 0; i < FieldNetwork::timeScales; ++i)
        timeScaleAlpha[(size_t) i] = (float) (1.0 - std::exp (-hopSeconds / (double) FieldNetwork::timeScaleSeconds[i]));
    for (int i = 0; i < FieldNetwork::spreads; ++i)
    {
        const float width = FieldNetwork::spreadBands[i];
        const int reach = (int) std::ceil (4.0f * width);
        for (int k = 0; k < count; ++k)
        {
            auto& row = spreadKernel[(size_t) i][(size_t) k];
            row.fill (0.0f);
            float sum = 0.0f;
            for (int j = std::max (0, k - reach); j <= std::min (count - 1, k + reach); ++j)
            {
                const float d = (float) (j - k) / width;
                row[(size_t) j] = std::exp (-0.5f * d * d);
                sum += row[(size_t) j];
            }
            for (auto& w : row)
                w /= sum;
        }
        spreadReach[(size_t) i] = reach;
    }

    // The field's fixed inputs don't change while playing, so their part is added up once.
    for (int m = 0; m < 2; ++m)
    {
        const auto& t = tuningFor ((Mode) m);
        const auto& net = *t.field;
        for (int k = 0; k < count; ++k)
        {
            const float place = placeInput (z[(size_t) k]);
            for (int h = 0; h < FieldNetwork::hidden; ++h)
            {
                float sum = net.inputBias[h] + net.inputWeights[kPlaceInput][h] * place;
                if (FieldNetwork::modeInput)
                    sum += net.inputWeights[kModeInput][h] * t.fieldModeInput;
                bandHidden[(size_t) m][(size_t) k][(size_t) h] = sum;
            }
            for (int o = 0; o < 2; ++o)
            {
                float sum = net.outputBias[o] + net.directWeights[kPlaceInput][o] * place;
                if (FieldNetwork::modeInput)
                    sum += net.directWeights[kModeInput][o] * t.fieldModeInput;
                bandOutput[(size_t) m][(size_t) k][(size_t) o] = sum;
            }
        }
    }

    setRangeMask (mask.data());
}

void PerceptualField::reset() noexcept
{
    primed = false;
    tracked = false;
    keepGuess = 0.0f;
    slowRecover.fill (0.0f);
    slowTame.fill (0.0f);
    recoverGain.fill (0.0f);
    tameGain.fill (0.0f);
}

void PerceptualField::track (const float* raw) noexcept
{
    for (int i = 0; i < FieldNetwork::timeScales; ++i)
    {
        auto& state = timeScaled[(size_t) i];
        const float a = timeScaleAlpha[(size_t) i];
        for (int k = 0; k < count; ++k)
            state[(size_t) k] = tracked ? state[(size_t) k] + a * (raw[k] - state[(size_t) k]) : raw[k];
    }
    tracked = true;
}

void PerceptualField::setRangeMask (const float* newMask) noexcept
{
    for (int k = 0; k < count; ++k)
        mask[(size_t) k] = newMask[k];
}

void PerceptualField::compute (const float* energy, const float* loudnessEnergy, const Controls& controls, Mode mode, float* gainDb) noexcept
{
    const auto& t = tuningFor (mode);

    float total = 1.0e-13f;
    for (int k = 0; k < count; ++k)
        total += mask[(size_t) k] * energy[k];
    const float totalDb = powToDb (total);

    // Relative level, with a floor: a band this far under the rest of the signal
    // holds nothing to reveal. Bands outside the range still count as neighbours.
    for (int k = 0; k < count; ++k)
        relative[(size_t) k] = levelInput (std::max (powToDb (energy[k] + 1.0e-13f) - totalDb, -kFloorDb));

    // The same over the other time scales, each against its own total, and spread
    // over the neighbouring bands.
    constexpr int extras = FieldNetwork::timeScales + FieldNetwork::spreads;
    std::array<BandArray, (size_t) std::max (1, extras)> extra;
    for (int i = 0; i < FieldNetwork::timeScales; ++i)
    {
        const float* e = tracked ? timeScaled[(size_t) i].data() : energy;
        float sum = 1.0e-13f;
        for (int k = 0; k < count; ++k)
            sum += mask[(size_t) k] * e[k];
        const float wholeDb = powToDb (sum);
        for (int k = 0; k < count; ++k)
            extra[(size_t) i][(size_t) k] = levelInput (std::max (powToDb (e[k] + 1.0e-13f) - wholeDb, -kFloorDb));
    }
    for (int i = 0; i < FieldNetwork::spreads; ++i)
    {
        const int reach = spreadReach[(size_t) i];
        for (int k = 0; k < count; ++k)
        {
            const auto& row = spreadKernel[(size_t) i][(size_t) k];
            float sum = 1.0e-13f;
            for (int j = std::max (0, k - reach); j <= std::min (count - 1, k + reach); ++j)
                sum += row[(size_t) j] * energy[j];
            extra[(size_t) (FieldNetwork::timeScales + i)][(size_t) k] = levelInput (std::max (powToDb (sum) - totalDb, -kFloorDb));
        }
    }

    const float brighten = controls.brighten;
    const auto& net = *t.field;
    const int m = mode == Mode::master ? 1 : 0;
    constexpr int levels = kLevelInputs;

    for (int k = 0; k < count; ++k)
    {
        // The band itself, then 3, 2 and 1 below it, then 1, 2 and 3 above it. Past
        // either end of the spectrum the last band stands in.
        float in[levels];
        in[0] = relative[(size_t) k];
        for (int i = 0; i < kFieldReach; ++i)
        {
            in[1 + i] = relative[(size_t) std::max (0, k - kFieldReach + i)];
            in[1 + kFieldReach + i] = relative[(size_t) std::min (count - 1, k + 1 + i)];
        }

        float first[kMaxHidden], second[kMaxHidden];
        const auto& fixed = bandHidden[(size_t) m][(size_t) k];
        for (int h = 0; h < FieldNetwork::hidden; ++h)
        {
            float sum = fixed[(size_t) h];
            for (int i = 0; i < levels; ++i)
                sum += in[i] * net.inputWeights[i][h];
            for (int e = 0; e < extras; ++e)
                sum += extra[(size_t) e][(size_t) k] * net.inputWeights[kExtraInputs + e][h];
            first[h] = fieldTanh (sum);
        }
        for (int h = 0; h < FieldNetwork::hidden; ++h)
        {
            float sum = net.hiddenBias[h];
            for (int i = 0; i < FieldNetwork::hidden; ++i)
                sum += first[i] * net.hiddenWeights[i][h];
            second[h] = fieldTanh (sum);
        }
        float forRecover = bandOutput[(size_t) m][(size_t) k][0];
        float forTame = bandOutput[(size_t) m][(size_t) k][1];
        for (int i = 0; i < levels; ++i)
        {
            forRecover += in[i] * net.directWeights[i][0];
            forTame += in[i] * net.directWeights[i][1];
        }
        for (int e = 0; e < extras; ++e)
        {
            forRecover += extra[(size_t) e][(size_t) k] * net.directWeights[kExtraInputs + e][0];
            forTame += extra[(size_t) e][(size_t) k] * net.directWeights[kExtraInputs + e][1];
        }
        for (int h = 0; h < FieldNetwork::hidden; ++h)
        {
            forRecover += second[h] * net.outputWeights[h][0];
            forTame += second[h] * net.outputWeights[h][1];
        }

        // Positive Brighten calms the treble's movement around its own average,
        // and only that, so the tilt alone decides which way the balance goes.
        auto& slowR = slowRecover[(size_t) k];
        auto& slowT = slowTame[(size_t) k];
        slowR = primed ? slowR + slowAlpha * (forRecover - slowR) : forRecover;
        slowT = primed ? slowT + slowAlpha * (forTame - slowT) : forTame;
        const float calm = std::max (0.0f, 1.0f - t.brightenTrebleSlope * brighten * trebleWeight[(size_t) k]);
        forRecover = slowR + (forRecover - slowR) * calm;
        forTame = slowT + (forTame - slowT) * calm;

        fieldRecover[(size_t) k] = forRecover;
        fieldTame[(size_t) k] = forTame;
    }
    primed = true;

    const float scaleRecover = amountScale (controls.recover, t);
    const float scaleTame = amountScale (controls.tame, t);
    const float scaleMean = 0.5f * (scaleRecover + scaleTame);
    const float shared = std::min (scaleRecover, scaleTame);
    const float soft2 = t.softnessDb * t.softnessDb;

    // Bias moves the line between the classes and softens one side's knee. The shared
    // part removes it entirely with Recover equal to Tame, as in the reference, because
    // then only the unbiased curves are left.
    const float line = -controls.bias * t.biasLineDb;
    const float wider = std::exp (t.biasKnee * controls.bias);
    const float softRecover2 = soft2 * wider * wider;
    const float softTame2 = soft2 / (wider * wider);

    // Levels off well before full travel, but stays smooth through zero.
    const float bent = (1.0f - std::exp (-std::abs (brighten) / kBrightenBend)) / (1.0f - std::exp (-1.0f / kBrightenBend));
    const float tiltAmount = scaleMean * (brighten < 0.0f ? -bent : bent);
    const float tiltLow = brighten < 0.0f ? t.darkenLowPerErb : t.tiltLowPerErb;
    const float tiltHigh = brighten < 0.0f ? t.darkenHighPerErb : t.tiltHighPerErb;
    const float tiltKnee = brighten < 0.0f ? kDarkenKneeErb : t.tiltKneeErb;

    float recoverBands = 0.0f, tameBands = 0.0f;
    for (int k = 0; k < count; ++k)
    {
        const float fr = fieldRecover[(size_t) k];
        const float ft = fieldTame[(size_t) k];
        const float up = above (fr, soft2);
        const float down = below (ft, soft2);
        const float upLine = above (fr - line, softRecover2);
        const float downLine = below (ft - line, softTame2);

        const float lifted = scaleRecover * upLine + shared * (up - upLine);
        const float cut = scaleTame * downLine + shared * (down - downLine);

        const float fromPivot = z[(size_t) k] - kPivotZ;
        const float tilt = fromPivot < 0.0f ? tiltLow * fromPivot
                                            : tiltHigh * tiltKnee * std::tanh (fromPivot / tiltKnee);
        const float g = lifted + cut + tiltAmount * tilt;

        recoverGain[(size_t) k] = mask[(size_t) k] * lifted;
        tameGain[(size_t) k] = mask[(size_t) k] * cut;
        shape[(size_t) k] = kGainLimitDb * std::tanh (g / kGainLimitDb);
        faded[(size_t) k] = topFade[(size_t) k] * shape[(size_t) k];

        if (fr + ft - 2.0f * line > 0.0f)
            recoverBands += mask[(size_t) k];
        else
            tameBands += mask[(size_t) k];
    }

    const float bandsTotal = recoverBands + tameBands;
    share = bandsTotal > 0.0f ? (recoverBands - tameBands) / bandsTotal : 0.0f;

    keepGuess = keepLoudnessDb (loudnessEnergy, faded.data(), mask.data(), count, kFieldLoudnessExponent, keepGuess);
    for (int k = 0; k < count; ++k)
        gainDb[k] = mask[(size_t) k] * topFade[(size_t) k] * (shape[(size_t) k] + keepGuess);
}

} // namespace colgate
