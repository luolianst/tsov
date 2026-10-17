#pragma once

#include <juce_audio_devices/juce_audio_devices.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include <atomic>
#include <cstdint>

namespace tsov
{
class AudioEngine final : public juce::AudioIODeviceCallback
{
public:
    struct Config
    {
        juce::String deviceSubstring;
        juce::String deviceTypeSubstring;
        double sampleRate = 48000.0;
        int blockSize = 128;
        double toneHz = 220.0;
        double gain = 0.02;
    };

    AudioEngine() = default;
    ~AudioEngine() override;

    bool open(const Config& config, juce::String& error);
    void close();

    // Live VST3 instrument (optional). Must be set before the device starts;
    // the engine prepares/releases it on the audio thread lifecycle.
    void setPlugin(std::unique_ptr<juce::AudioPluginInstance> instance);
    juce::String pluginName() const;
    float pluginPeak() const { return pluginPeakValue.load(); }

    // No audio device: run the same render path synchronously (CI / offline smoke).
    void runNull(int blocks);

    void audioDeviceIOCallbackWithContext(const float* const* inputChannelData,
                                          int numInputChannels,
                                          float* const* outputChannelData,
                                          int numOutputChannels,
                                          int numSamples,
                                          const juce::AudioIODeviceCallbackContext& context) override;
    void audioDeviceAboutToStart(juce::AudioIODevice* device) override;
    void audioDeviceStopped() override;

    juce::String deviceName() const;
    juce::String deviceTypeName() const;
    double sampleRate() const { return actualSampleRate.load(); }
    int blockSize() const { return actualBlockSize.load(); }
    int64_t framesRendered() const { return frames.load(); }
    int callbackCount() const { return callbacks.load(); }
    int xrunCount() const;
    int outputLatencySamples() const;
    juce::Array<double> availableSampleRates() const;
    juce::Array<int> availableBufferSizes() const;

private:
    void renderBlock(juce::AudioBuffer<float>& buffer, int numSamples);

    juce::AudioDeviceManager deviceManager;
    Config cfg;

    std::atomic<double> actualSampleRate { 0.0 };
    std::atomic<int> actualBlockSize { 0 };
    std::atomic<int64_t> frames { 0 };
    std::atomic<int> callbacks { 0 };

    double phase = 0.0;
    double phaseIncrement = 0.0;
    bool running = false;

    std::unique_ptr<juce::AudioPluginInstance> plugin;
    juce::AudioBuffer<float> pluginBuffer;
    juce::MidiBuffer pluginMidi;
    std::atomic<float> pluginPeakValue { 0.0f };
    int64_t noteCycleSamples = 0;
};
} // namespace tsov
