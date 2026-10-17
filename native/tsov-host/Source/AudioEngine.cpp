#include "AudioEngine.h"

#include <algorithm>
#include <cmath>

namespace
{
double chooseSampleRate(const juce::Array<double>& rates, double requested)
{
    if (rates.isEmpty() || rates.contains(requested))
        return requested;
    return *std::min_element(rates.begin(), rates.end(), [requested](double a, double b)
    {
        return std::abs(a - requested) < std::abs(b - requested);
    });
}

int chooseBufferSize(const juce::Array<int>& sizes, int requested)
{
    if (sizes.isEmpty() || sizes.contains(requested))
        return requested;
    auto it = std::find_if(sizes.begin(), sizes.end(), [requested](int size) { return size >= requested; });
    return it != sizes.end() ? *it : sizes.getLast();
}
} // namespace

namespace tsov
{
AudioEngine::~AudioEngine()
{
    close();
}

bool AudioEngine::open(const Config& config, juce::String& error)
{
    cfg = config;

    const auto initError = deviceManager.initialiseWithDefaultDevices(0, 2);
    if (initError.isNotEmpty())
    {
        error = "initialiseWithDefaultDevices: " + initError;
        return false;
    }

    if (cfg.deviceTypeSubstring.isNotEmpty())
    {
        juce::String matchedType;
        auto& types = deviceManager.getAvailableDeviceTypes();
        for (int i = 0; i < types.size(); ++i)
        {
            if (types[i]->getTypeName().containsIgnoreCase(cfg.deviceTypeSubstring))
            {
                matchedType = types[i]->getTypeName();
                break;
            }
        }

        if (matchedType.isEmpty())
        {
            error = "audio device type not found: " + cfg.deviceTypeSubstring;
            deviceManager.closeAudioDevice();
            return false;
        }

        deviceManager.setCurrentAudioDeviceType(matchedType, true);
    }

    auto* device = deviceManager.getCurrentAudioDevice();
    if (device == nullptr)
    {
        error = "no audio device";
        return false;
    }

    const auto targetRate = chooseSampleRate(device->getAvailableSampleRates(), cfg.sampleRate);
    const auto targetBlock = chooseBufferSize(device->getAvailableBufferSizes(), cfg.blockSize);

    if (cfg.deviceSubstring.isNotEmpty())
    {
        juce::String matched;
        auto& types = deviceManager.getAvailableDeviceTypes();
        for (int i = 0; i < types.size() && matched.isEmpty(); ++i)
        {
            auto& type = *types[i];
            type.scanForDevices();
            for (const auto& name : type.getDeviceNames(false))
            {
                if (name.containsIgnoreCase(cfg.deviceSubstring))
                {
                    matched = name;
                    break;
                }
            }
        }

        if (matched.isEmpty())
        {
            error = "output device not found: " + cfg.deviceSubstring;
            deviceManager.closeAudioDevice();
            return false;
        }

        auto setup = deviceManager.getAudioDeviceSetup();
        setup.outputDeviceName = matched;
        setup.sampleRate = targetRate;
        setup.bufferSize = targetBlock;
        const auto setupError = deviceManager.setAudioDeviceSetup(setup, true);
        if (setupError.isNotEmpty())
        {
            error = "setAudioDeviceSetup: " + setupError;
            deviceManager.closeAudioDevice();
            return false;
        }
    }
    else
    {
        auto setup = deviceManager.getAudioDeviceSetup();
        setup.sampleRate = targetRate;
        setup.bufferSize = targetBlock;
        const auto setupError = deviceManager.setAudioDeviceSetup(setup, true);
        if (setupError.isNotEmpty())
        {
            // Keep the default device; report the requested format failure via the error string
            // only if the device did not open at all.
            error = "requested format not applied: " + setupError;
        }
    }

    device = deviceManager.getCurrentAudioDevice();
    if (device == nullptr)
    {
        if (error.isEmpty())
            error = "audio device closed during setup";
        return false;
    }

    actualSampleRate = device->getCurrentSampleRate();
    actualBlockSize = device->getCurrentBufferSizeSamples();
    phase = 0.0;
    phaseIncrement = cfg.toneHz > 0.0 ? juce::MathConstants<double>::twoPi * cfg.toneHz / actualSampleRate.load() : 0.0;
    deviceManager.addAudioCallback(this);
    running = true;
    return true;
}

void AudioEngine::close()
{
    if (running)
    {
        deviceManager.removeAudioCallback(this);
        if (plugin != nullptr)
            plugin->releaseResources();
        deviceManager.closeAudioDevice();
        running = false;
    }
}

void AudioEngine::setPlugin(std::unique_ptr<juce::AudioPluginInstance> instance)
{
    plugin = std::move(instance);
    pluginPeakValue = 0.0f;
    noteCycleSamples = 0;
    if (plugin != nullptr && actualSampleRate.load() > 0.0 && actualBlockSize.load() > 0)
        plugin->prepareToPlay(actualSampleRate.load(), actualBlockSize.load());
}

juce::String AudioEngine::pluginName() const
{
    return plugin != nullptr ? plugin->getName() : juce::String();
}

void AudioEngine::runNull(int blocks)
{
    actualSampleRate = cfg.sampleRate;
    actualBlockSize = cfg.blockSize;
    phase = 0.0;
    phaseIncrement = cfg.toneHz > 0.0 ? juce::MathConstants<double>::twoPi * cfg.toneHz / cfg.sampleRate : 0.0;
    noteCycleSamples = (int64_t) cfg.sampleRate;
    pluginPeakValue = 0.0f;

    if (plugin != nullptr)
    {
        plugin->prepareToPlay(cfg.sampleRate, cfg.blockSize);
        pluginBuffer.setSize(juce::jmax(2, plugin->getTotalNumOutputChannels()), cfg.blockSize);
    }

    juce::AudioBuffer<float> buffer(2, cfg.blockSize);
    for (int i = 0; i < blocks; ++i)
    {
        renderBlock(buffer, cfg.blockSize);
        frames += cfg.blockSize;
        callbacks += 1;
    }
}

void AudioEngine::audioDeviceIOCallbackWithContext(const float* const*,
                                                    int,
                                                    float* const* outputChannelData,
                                                    int numOutputChannels,
                                                    int numSamples,
                                                    const juce::AudioIODeviceCallbackContext&)
{
    if (numOutputChannels <= 0 || outputChannelData == nullptr)
        return;

    juce::AudioBuffer<float> buffer(outputChannelData, numOutputChannels, numSamples);
    renderBlock(buffer, numSamples);
    frames += numSamples;
    callbacks += 1;
}

void AudioEngine::audioDeviceAboutToStart(juce::AudioIODevice* device)
{
    if (device == nullptr)
        return;
    actualSampleRate = device->getCurrentSampleRate();
    actualBlockSize = device->getCurrentBufferSizeSamples();
    phase = 0.0;
    phaseIncrement = cfg.toneHz > 0.0 ? juce::MathConstants<double>::twoPi * cfg.toneHz / actualSampleRate.load() : 0.0;
    noteCycleSamples = (int64_t) actualSampleRate.load();
    pluginPeakValue = 0.0f;

    if (plugin != nullptr)
    {
        plugin->prepareToPlay(actualSampleRate.load(), actualBlockSize.load());
        pluginBuffer.setSize(juce::jmax(2, plugin->getTotalNumOutputChannels()), actualBlockSize.load());
    }
}

void AudioEngine::audioDeviceStopped()
{
}

void AudioEngine::renderBlock(juce::AudioBuffer<float>& buffer, int numSamples)
{
    buffer.clear();

    if (plugin != nullptr)
    {
        if (pluginBuffer.getNumChannels() < 2 || pluginBuffer.getNumSamples() < numSamples)
            pluginBuffer.setSize(juce::jmax(2, plugin->getTotalNumOutputChannels()), numSamples);

        pluginBuffer.clear();
        pluginMidi.clear();

        const auto startFrame = frames.load();
        const auto cycle = noteCycleSamples > 0 ? noteCycleSamples : (int64_t) actualSampleRate.load();
        for (int i = 0; i < numSamples; ++i)
        {
            const auto phaseInCycle = (startFrame + i) % cycle;
            if (phaseInCycle == 0)
                pluginMidi.addEvent(juce::MidiMessage::noteOn(1, 60, 0.8f), i);
            else if (phaseInCycle == cycle / 2)
                pluginMidi.addEvent(juce::MidiMessage::noteOff(1, 60), i);
        }

        plugin->processBlock(pluginBuffer, pluginMidi);

        for (int channel = 0; channel < buffer.getNumChannels(); ++channel)
        {
            const auto sourceChannel = juce::jmin(channel, pluginBuffer.getNumChannels() - 1);
            buffer.copyFrom(channel, 0, pluginBuffer, sourceChannel, 0, numSamples);
        }

        const auto blockPeak = pluginBuffer.getMagnitude(0, numSamples);
        if (blockPeak > pluginPeakValue.load())
            pluginPeakValue = blockPeak;
        return;
    }

    if (phaseIncrement <= 0.0)
        return;

    const auto gain = static_cast<float>(cfg.gain);
    for (int channel = 0; channel < buffer.getNumChannels(); ++channel)
    {
        auto* out = buffer.getWritePointer(channel);
        auto p = phase;
        for (int i = 0; i < numSamples; ++i)
        {
            out[i] = gain * static_cast<float>(std::sin(p));
            p += phaseIncrement;
            if (p >= juce::MathConstants<double>::twoPi)
                p -= juce::MathConstants<double>::twoPi;
        }
    }
    phase += phaseIncrement * numSamples;
    while (phase >= juce::MathConstants<double>::twoPi)
        phase -= juce::MathConstants<double>::twoPi;
}

juce::String AudioEngine::deviceName() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getName();
    return {};
}

juce::String AudioEngine::deviceTypeName() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getTypeName();
    return {};
}

int AudioEngine::xrunCount() const
{
    return deviceManager.getXRunCount();
}

int AudioEngine::outputLatencySamples() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getOutputLatencyInSamples();
    return 0;
}

juce::Array<double> AudioEngine::availableSampleRates() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getAvailableSampleRates();
    return {};
}

juce::Array<int> AudioEngine::availableBufferSizes() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getAvailableBufferSizes();
    return {};
}
} // namespace tsov
