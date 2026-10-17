#pragma once

#include <juce_audio_devices/juce_audio_devices.h>
#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include <atomic>
#include <cstdint>
#include <memory>
#include <vector>

namespace tsov
{
struct MixNote
{
    int64_t startSample = 0;
    int64_t endSample = 0;
    int note = 60;
    float velocity = 0.8f;
};

struct MixTrackInfo
{
    juce::String name;
    float gain = 1.0f;
    float pan = 0.0f;
    bool mute = false;
    int noteCount = 0;
};

struct MixTimedEvent
{
    int64_t sample = 0;
    juce::MidiMessage message;
};

// Multi-track real-time engine used by the v0.2 live path.
// One AudioIODeviceCallback owns all instrument tracks and a simple
// track -> master mix bus. MIDI events are scheduled sample-accurately.
class MixEngine final : public juce::AudioIODeviceCallback
{
public:
    struct Config
    {
        juce::String deviceSubstring;
        juce::String deviceTypeSubstring;
        double sampleRate = 48000.0;
        int blockSize = 128;
        int inputChannels = 0;
        bool monitorInput = false;
        double masterGain = 1.0;
    };

    MixEngine() = default;
    ~MixEngine() override;

    bool open(const Config& config, juce::String& error);
    void close();

    int addTrack(std::unique_ptr<juce::AudioPluginInstance> instance,
                 const juce::String& name = {},
                 float gain = 1.0f,
                 float pan = 0.0f,
                 int noteOffset = 0,
                 bool testPattern = false);
    void clearTracks();
    int trackCount() const { return (int) tracks.size(); }
    MixTrackInfo trackInfo(int index) const;
    float trackPeak(int index) const;
    void setTrackGain(int index, float gain);
    void setTrackPan(int index, float pan);
    void setTrackMute(int index, bool mute);
    void setNotes(int index, std::vector<MixNote> notes);

    void play();
    void stop();
    bool isPlaying() const { return playing.load(); }
    int64_t playheadSamples() const { return playhead.load(); }

    // Records the active input channels to a 24-bit WAV via a background writer.
    bool startRecording(const juce::File& file, int numChannels, juce::String& error);
    void stopRecording();
    bool isRecording() const { return recording.load(); }
    int64_t recordedFrames() const { return recordFrames.load(); }
    int recordingDropouts() const { return recordDropouts.load(); }

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
    float inputPeak() const { return inputPeakValue.load(); }
    float masterPeak() const { return masterPeakValue.load(); }
    juce::Array<double> availableSampleRates() const;
    juce::Array<int> availableBufferSizes() const;

private:
    struct Track
    {
        juce::String name;
        std::unique_ptr<juce::AudioPluginInstance> plugin;
        juce::AudioBuffer<float> buffer;
        juce::MidiBuffer midi;
        std::vector<MixTimedEvent> events;
        size_t eventCursor = 0;
        std::vector<MixNote> notes;
        std::atomic<float> peak { 0.0f };
        float gain = 1.0f;
        float pan = 0.0f;
        bool mute = false;
        bool testPattern = false;
        int noteOffset = 0;
    };

    void prepareTracks(double sampleRate, int blockSize);
    void releaseTracks();
    void buildTestPattern(Track& track, double sampleRate);
    void renderBlock(juce::AudioBuffer<float>& buffer, int numSamples);

    juce::AudioDeviceManager deviceManager;
    Config cfg;

    std::atomic<double> actualSampleRate { 0.0 };
    std::atomic<int> actualBlockSize { 0 };
    std::atomic<int64_t> frames { 0 };
    std::atomic<int> callbacks { 0 };
    std::atomic<int64_t> playhead { 0 };
    std::atomic<bool> playing { false };
    std::atomic<float> inputPeakValue { 0.0f };
    std::atomic<float> masterPeakValue { 0.0f };

    bool running = false;
    std::vector<std::unique_ptr<Track>> tracks;

    juce::TimeSliceThread recordThread { "tsov-record" };
    std::unique_ptr<juce::AudioFormatWriter::ThreadedWriter> recordWriter;
    juce::AudioBuffer<float> recordBuffer;
    std::atomic<bool> recording { false };
    std::atomic<int64_t> recordFrames { 0 };
    std::atomic<int> recordDropouts { 0 };
    int recordChannels = 0;
};
} // namespace tsov
