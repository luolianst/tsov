#pragma once

#include <juce_core/juce_core.h>

#include <cstdint>

namespace tsov
{
// Minimal real-time SF2 source backed by the bundled FluidSynth DLL.
// One instance owns one FluidSynth synth/program; the engine renders each
// SF2 track independently (documented memory trade-off until a shared bank lands).
class SF2Synth
{
public:
    SF2Synth() = default;
    ~SF2Synth();

    bool load(const juce::String& soundfont,
              int bank,
              int program,
              double sampleRate,
              const juce::String& libraryPath,
              juce::String& error);
    bool prepare(double sampleRate, juce::String& error);

    void noteOn(int note, float velocity);
    void noteOff(int note);
    void allNotesOff();
    void render(float* left, float* right, int numSamples);

private:
    void close();

    void* settings = nullptr;
    void* synth = nullptr;
    juce::String soundfontPath;
    juce::String dllPath;
    int bankNumber = 0;
    int programNumber = 0;
    double currentSampleRate = 0.0;
};
} // namespace tsov
