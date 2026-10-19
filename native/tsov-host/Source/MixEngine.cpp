#include "MixEngine.h"

#include <algorithm>
#include <cmath>
#include <iostream>

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

void addEvent(std::vector<tsov::MixTimedEvent>& events, int64_t sample, juce::MidiMessage message)
{
    events.push_back({ sample, std::move(message) });
}
} // namespace

namespace tsov
{
MixEngine::~MixEngine()
{
    close();
}

bool MixEngine::open(const Config& config, juce::String& error)
{
    cfg = config;

    auto initError = deviceManager.initialiseWithDefaultDevices(cfg.inputChannels, 2);
    if (initError.isNotEmpty() && cfg.inputChannels > 0)
    {
        // Keep the output path usable even when no input device is available.
        initError = deviceManager.initialiseWithDefaultDevices(0, 2);
        if (initError.isEmpty())
            error = "input unavailable; opened output only";
    }
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
            error = "requested format not applied: " + setupError;
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
    deviceManager.addAudioCallback(this);
    running = true;
    return true;
}

void MixEngine::close()
{
    if (running)
    {
        deviceManager.removeAudioCallback(this);
        stopRecording();
        releaseTracks();
        deviceManager.closeAudioDevice();
        running = false;
    }
}

void MixEngine::releaseTracks()
{
    for (auto& track : tracks)
    {
        if (track->plugin != nullptr)
            track->plugin->releaseResources();
    }
}

int MixEngine::addTrack(std::unique_ptr<juce::AudioPluginInstance> instance,
                        const juce::String& name,
                        float gain,
                        float pan,
                        int noteOffset,
                        bool testPattern)
{
    if (instance == nullptr)
        return -1;

    auto track = std::make_unique<Track>();
    track->name = name.isEmpty() ? instance->getName() : name;
    track->plugin = std::move(instance);
    track->gain = gain;
    track->pan = juce::jlimit(-1.0f, 1.0f, pan);
    track->testPattern = testPattern;
    track->noteOffset = noteOffset;

    const auto sampleRateNow = actualSampleRate.load();
    const auto blockSizeNow = actualBlockSize.load();
    if (sampleRateNow > 0.0 && blockSizeNow > 0)
    {
        track->plugin->prepareToPlay(sampleRateNow, blockSizeNow);
        track->buffer.setSize(juce::jmax(2, track->plugin->getTotalNumOutputChannels()), blockSizeNow);
        if (track->testPattern)
            buildTestPattern(*track, sampleRateNow);
    }

    tracks.push_back(std::move(track));
    return (int) tracks.size() - 1;
}

int MixEngine::addSf2Track(std::unique_ptr<SF2Synth> synth,
                           const juce::String& name,
                           float gain,
                           float pan,
                           int noteOffset,
                           bool testPattern)
{
    if (synth == nullptr)
        return -1;

    auto track = std::make_unique<Track>();
    track->name = name.isEmpty() ? juce::String("SF2") : name;
    track->sf2 = std::move(synth);
    track->gain = gain;
    track->pan = juce::jlimit(-1.0f, 1.0f, pan);
    track->testPattern = testPattern;
    track->noteOffset = noteOffset;
    track->buffer.setSize(2, 512);

    const auto sampleRateNow = actualSampleRate.load();
    const auto blockSizeNow = actualBlockSize.load();
    if (sampleRateNow > 0.0 && blockSizeNow > 0)
    {
        juce::String error;
        if (! track->sf2->prepare(sampleRateNow, error))
            std::cerr << "SF2 prepare failed: " << error << std::endl;
        track->buffer.setSize(2, blockSizeNow);
        if (track->testPattern)
            buildTestPattern(*track, sampleRateNow);
    }

    tracks.push_back(std::move(track));
    return (int) tracks.size() - 1;
}

int MixEngine::addAudioTrack(std::unique_ptr<juce::AudioBuffer<float>> audio,
                             const juce::String& name,
                             float gain,
                             float pan)
{
    if (audio == nullptr || audio->getNumSamples() <= 0)
        return -1;

    auto track = std::make_unique<Track>();
    track->name = name.isEmpty() ? juce::String("Audio") : name;
    track->audio = std::move(audio);
    track->gain = gain;
    track->pan = juce::jlimit(-1.0f, 1.0f, pan);
    track->buffer.setSize(2, 512);
    tracks.push_back(std::move(track));
    return (int) tracks.size() - 1;
}

void MixEngine::clearTracks()
{
    releaseTracks();
    tracks.clear();
}

MixTrackInfo MixEngine::trackInfo(int index) const
{
    MixTrackInfo info;
    if (index < 0 || index >= (int) tracks.size())
        return info;

    const auto& track = *tracks[(size_t) index];
    const juce::SpinLock::ScopedLockType lock(track.eventsLock);
    info.name = track.name;
    info.gain = track.gain;
    info.pan = track.pan;
    info.mute = track.mute;
    info.noteCount = (int) track.notes.size();
    return info;
}

float MixEngine::trackPeak(int index) const
{
    if (index < 0 || index >= (int) tracks.size())
        return 0.0f;
    return tracks[(size_t) index]->peak.load();
}

void MixEngine::setTrackGain(int index, float gain)
{
    if (index >= 0 && index < (int) tracks.size())
        tracks[(size_t) index]->gain.store(juce::jlimit(0.0f, 2.0f, gain));
}

void MixEngine::setTrackPan(int index, float pan)
{
    if (index >= 0 && index < (int) tracks.size())
        tracks[(size_t) index]->pan.store(juce::jlimit(-1.0f, 1.0f, pan));
}

void MixEngine::setTrackMute(int index, bool mute)
{
    if (index >= 0 && index < (int) tracks.size())
        tracks[(size_t) index]->mute.store(mute);
}

void MixEngine::setNotes(int index, std::vector<MixNote> notes)
{
    if (index < 0 || index >= (int) tracks.size())
        return;

    auto& track = *tracks[(size_t) index];
    const juce::SpinLock::ScopedLockType lock(track.eventsLock);
    track.notes = std::move(notes);
    track.events.clear();
    track.eventCursor = 0;
    for (const auto& note : track.notes)
    {
        if (note.endSample <= note.startSample)
            continue;
        addEvent(track.events, note.startSample,
                 juce::MidiMessage::noteOn(1, note.note, juce::jlimit(0.0f, 1.0f, note.velocity)));
        addEvent(track.events, note.endSample,
                 juce::MidiMessage::noteOff(1, note.note));
    }

    std::stable_sort(track.events.begin(), track.events.end(), [](const MixTimedEvent& a, const MixTimedEvent& b)
    {
        if (a.sample != b.sample)
            return a.sample < b.sample;
        return a.message.isNoteOff() && !b.message.isNoteOff();
    });

    // Live edits only affect future triggers (N1): skip events already behind
    // the current playhead instead of replaying the whole track.
    const auto target = playhead.load();
    track.eventCursor = (size_t) std::distance(
        track.events.begin(),
        std::lower_bound(track.events.begin(), track.events.end(), target,
                         [](const MixTimedEvent& event, int64_t value)
                         {
                             return event.sample < value;
                         }));
}

void MixEngine::buildTestPattern(Track& track, double sampleRate)
{
    track.events.clear();
    track.notes.clear();
    track.eventCursor = 0;
    const auto cycle = (int64_t) juce::jmax(1.0, sampleRate);
    for (int i = 0; i < 32; ++i)
    {
        MixNote note;
        note.startSample = (int64_t) i * cycle;
        note.endSample = note.startSample + cycle / 2;
        note.note = juce::jlimit(0, 127, 60 + track.noteOffset);
        note.velocity = 0.8f;
        track.notes.push_back(note);
        addEvent(track.events, note.startSample, juce::MidiMessage::noteOn(1, note.note, note.velocity));
        addEvent(track.events, note.endSample, juce::MidiMessage::noteOff(1, note.note));
    }

    std::stable_sort(track.events.begin(), track.events.end(), [](const MixTimedEvent& a, const MixTimedEvent& b)
    {
        if (a.sample != b.sample)
            return a.sample < b.sample;
        return a.message.isNoteOff() && !b.message.isNoteOff();
    });
}

void MixEngine::prepareTracks(double sampleRate, int blockSize)
{
    for (auto& track : tracks)
    {
        if (track->audio != nullptr)
        {
            track->buffer.setSize(2, blockSize);
        }
        else if (track->sf2 != nullptr)
        {
            juce::String error;
            if (! track->sf2->prepare(sampleRate, error))
                std::cerr << "SF2 prepare failed: " << error << std::endl;
            track->buffer.setSize(2, blockSize);
        }
        else if (track->plugin != nullptr)
        {
            track->plugin->prepareToPlay(sampleRate, blockSize);
            track->buffer.setSize(juce::jmax(2, track->plugin->getTotalNumOutputChannels()), blockSize);
        }
        track->buffer.clear();
        track->peak = 0.0f;
        track->eventCursor = 0;
        if (track->testPattern)
            buildTestPattern(*track, sampleRate);
    }
}

void MixEngine::play()
{
    playhead = 0;
    for (auto& track : tracks)
    {
        const juce::SpinLock::ScopedLockType lock(track->eventsLock);
        track->eventCursor = 0;
    }
    playing = true;
}

void MixEngine::resume()
{
    playing = true;
}

void MixEngine::seek(int64_t sample)
{
    const auto target = juce::jmax<int64_t>(0, sample);
    playhead = target;
    resetEventCursorsTo(target);
}

void MixEngine::setLoop(int64_t startSample, int64_t endSample)
{
    if (endSample <= startSample)
    {
        clearLoop();
        return;
    }
    loopStart = juce::jmax<int64_t>(0, startSample);
    loopEnd = endSample;
}

void MixEngine::clearLoop()
{
    loopStart = -1;
    loopEnd = -1;
}

void MixEngine::resetEventCursorsTo(int64_t sample)
{
    for (auto& track : tracks)
    {
        const juce::SpinLock::ScopedLockType lock(track->eventsLock);
        track->eventCursor = (size_t) std::distance(
            track->events.begin(),
            std::lower_bound(track->events.begin(), track->events.end(), sample,
                             [](const MixTimedEvent& event, int64_t value)
                             {
                                 return event.sample < value;
                             }));
        if (track->sf2 != nullptr)
            track->sf2->allNotesOff();
    }
}

void MixEngine::stop()
{
    playing = false;
}

bool MixEngine::startRecording(const juce::File& file, int numChannels, juce::String& error)
{
    stopRecording();

    const auto sampleRateNow = actualSampleRate.load();
    if (sampleRateNow <= 0.0)
    {
        error = "audio device is not running";
        return false;
    }

    recordChannels = juce::jlimit(1, 2, numChannels);
    file.getParentDirectory().createDirectory();
    auto stream = file.createOutputStream();
    if (stream == nullptr)
    {
        error = "cannot create output file: " + file.getFullPathName();
        return false;
    }

    juce::WavAudioFormat format;
    auto* writer = format.createWriterFor(stream.release(),
                                          sampleRateNow,
                                          (unsigned int) recordChannels,
                                          24,
                                          juce::StringPairArray(),
                                          0);
    if (writer == nullptr)
    {
        error = "WAV writer creation failed";
        return false;
    }

    recordThread.startThread();
    recordWriter = std::make_unique<juce::AudioFormatWriter::ThreadedWriter>(writer, recordThread, 32768);
    recordBuffer.setSize(recordChannels, juce::jmax(1, actualBlockSize.load()));
    recordFrames = 0;
    recordDropouts = 0;
    recording = true;
    return true;
}

void MixEngine::stopRecording()
{
    recording = false;
    recordWriter.reset();
    if (recordThread.isThreadRunning())
        recordThread.stopThread(1000);
    recordBuffer.setSize(0, 0);
    recordChannels = 0;
}

void MixEngine::runNull(int blocks)
{
    actualSampleRate = cfg.sampleRate;
    actualBlockSize = cfg.blockSize;
    prepareTracks(cfg.sampleRate, cfg.blockSize);
    playhead = 0;
    playing = true;

    juce::AudioBuffer<float> buffer(2, cfg.blockSize);
    for (int i = 0; i < blocks; ++i)
    {
        renderBlock(buffer, cfg.blockSize);
        frames += cfg.blockSize;
        playhead += cfg.blockSize;
        callbacks += 1;
    }
}

void MixEngine::audioDeviceAboutToStart(juce::AudioIODevice* device)
{
    if (device == nullptr)
        return;
    actualSampleRate = device->getCurrentSampleRate();
    actualBlockSize = device->getCurrentBufferSizeSamples();
    prepareTracks(actualSampleRate.load(), actualBlockSize.load());

    // Plugins often allocate/reset DSP state on their first processBlock. Do one
    // silent block now, before the transport starts, so the first audible block
    // does not overrun the device buffer (observed as a single xrun at ~90 ms).
    if (! tracks.empty() && actualBlockSize.load() > 0)
    {
        juce::AudioBuffer<float> warmup(2, actualBlockSize.load());
        const auto wasPlaying = playing.load();
        playing = true;
        renderBlock(warmup, actualBlockSize.load());
        playing = wasPlaying;
        playhead = 0;
        resetEventCursorsTo(0);
    }
}

void MixEngine::audioDeviceStopped()
{
}

void MixEngine::audioDeviceIOCallbackWithContext(const float* const* inputChannelData,
                                                 int numInputChannels,
                                                 float* const* outputChannelData,
                                                 int numOutputChannels,
                                                 int numSamples,
                                                 const juce::AudioIODeviceCallbackContext&)
{
    if (numOutputChannels <= 0 || outputChannelData == nullptr)
        return;

    if (inputChannelData != nullptr && numInputChannels > 0)
    {
        float peak = 0.0f;
        for (int channel = 0; channel < numInputChannels; ++channel)
        {
            const auto* data = inputChannelData[channel];
            if (data == nullptr)
                continue;
            for (int i = 0; i < numSamples; ++i)
                peak = juce::jmax(peak, std::abs(data[i]));
        }
        if (peak > inputPeakValue.load())
            inputPeakValue = peak;

        if (recording.load() && recordWriter != nullptr)
        {
            if (recordBuffer.getNumSamples() < numSamples || recordBuffer.getNumChannels() != recordChannels)
                recordBuffer.setSize(recordChannels, numSamples, false, false, true);
            recordBuffer.clear();
            for (int channel = 0; channel < recordChannels; ++channel)
            {
                const auto sourceChannel = juce::jmin(channel, numInputChannels - 1);
                const auto* input = inputChannelData[sourceChannel];
                if (input != nullptr)
                    recordBuffer.copyFrom(channel, 0, input, numSamples);
            }

            if (recordWriter->write(recordBuffer.getArrayOfReadPointers(), numSamples))
                recordFrames += numSamples;
            else
                recordDropouts += 1;
        }
    }

    int rendered = 0;
    while (rendered < numSamples)
    {
        auto loopBegin = loopStart.load();
        auto loopFinish = loopEnd.load();
        if (playing.load() && loopFinish > loopBegin && playhead.load() >= loopFinish)
        {
            playhead = loopBegin;
            resetEventCursorsTo(loopBegin);
        }

        int chunk = numSamples - rendered;
        if (playing.load() && loopFinish > loopBegin && playhead.load() < loopFinish)
            chunk = (int) juce::jmin<int64_t>((int64_t) chunk, loopFinish - playhead.load());

        if (chunk <= 0)
            break;

        juce::AudioBuffer<float> segment(outputChannelData, numOutputChannels, rendered, chunk);
        renderBlock(segment, chunk);
        rendered += chunk;
        if (playing.load())
            playhead += chunk;
    }

    if (cfg.monitorInput && inputChannelData != nullptr && numInputChannels > 0)
    {
        for (int channel = 0; channel < numOutputChannels; ++channel)
        {
            const auto sourceChannel = juce::jmin(channel, numInputChannels - 1);
            const auto* input = inputChannelData[sourceChannel];
            if (input == nullptr)
                continue;
            auto* output = outputChannelData[channel];
            for (int i = 0; i < numSamples; ++i)
                output[i] += input[i] * 0.8f;
        }
    }

    frames += numSamples;
    callbacks += 1;
}

void MixEngine::renderBlock(juce::AudioBuffer<float>& buffer, int numSamples)
{
    buffer.clear();

    if (playing.load() && !tracks.empty())
    {
        const auto start = playhead.load();
        const auto blockEnd = start + numSamples;

        for (auto& track : tracks)
        {
            if (track->mute || (track->plugin == nullptr && track->sf2 == nullptr && track->audio == nullptr))
                continue;

            const juce::SpinLock::ScopedTryLockType eventLock(track->eventsLock);
            if (! eventLock.isLocked())
                continue;

            const auto sourceChannelsWanted = track->plugin != nullptr
                ? juce::jmax(2, track->plugin->getTotalNumOutputChannels())
                : 2;
            if (track->buffer.getNumChannels() < 2 || track->buffer.getNumSamples() < numSamples)
                track->buffer.setSize(sourceChannelsWanted, numSamples);

            track->buffer.clear();
            track->midi.clear();

            while (track->eventCursor < track->events.size()
                   && track->events[track->eventCursor].sample < blockEnd)
            {
                const auto& event = track->events[track->eventCursor];
                const auto offset = (int) juce::jlimit<int64_t>((int64_t) 0, (int64_t) (numSamples - 1), event.sample - start);
                track->midi.addEvent(event.message, offset);
                ++track->eventCursor;
            }

            if (track->audio != nullptr)
            {
                const auto* asset = track->audio.get();
                const auto assetChannels = asset->getNumChannels();
                const auto assetSamples = asset->getNumSamples();
                auto* left = track->buffer.getWritePointer(0);
                auto* right = track->buffer.getWritePointer(1);
                for (int i = 0; i < numSamples; ++i)
                {
                    const auto sourceIndex = start + i;
                    if (sourceIndex < 0 || sourceIndex >= assetSamples)
                        continue;
                    const auto l = asset->getSample(0, (int) sourceIndex);
                    left[i] = l;
                    right[i] = assetChannels > 1 ? asset->getSample(1, (int) sourceIndex) : l;
                }
            }
            else if (track->sf2 != nullptr)
            {
                auto* left = track->buffer.getWritePointer(0);
                auto* right = track->buffer.getWritePointer(1);
                int cursor = 0;
                for (const auto metadata : track->midi)
                {
                    const auto offset = juce::jlimit(0, numSamples, metadata.samplePosition);
                    if (offset > cursor)
                    {
                        track->sf2->render(left + cursor, right + cursor, offset - cursor);
                        cursor = offset;
                    }
                    const auto message = metadata.getMessage();
                    if (message.isNoteOn())
                        track->sf2->noteOn(message.getNoteNumber(), message.getFloatVelocity());
                    else if (message.isNoteOff())
                        track->sf2->noteOff(message.getNoteNumber());
                }
                if (cursor < numSamples)
                    track->sf2->render(left + cursor, right + cursor, numSamples - cursor);
            }
            else
            {
                track->plugin->processBlock(track->buffer, track->midi);
            }

            const auto peak = track->buffer.getMagnitude(0, numSamples);
            if (peak > track->peak.load())
                track->peak = peak;

            const auto angle = (track->pan.load() + 1.0f) * juce::MathConstants<float>::pi * 0.25f;
            const auto leftGain = track->gain.load() * std::cos(angle);
            const auto rightGain = track->gain.load() * std::sin(angle);

            const auto sourceChannels = track->buffer.getNumChannels();
            for (int channel = 0; channel < buffer.getNumChannels(); ++channel)
            {
                const auto sourceChannel = juce::jmin(channel, sourceChannels - 1);
                const auto gain = channel == 0 ? leftGain
                                               : (channel == 1 ? rightGain : track->gain.load());
                buffer.addFrom(channel, 0, track->buffer, sourceChannel, 0, numSamples, gain);
            }
        }
    }

    const auto master = (float) cfg.masterGain;
    if (master != 1.0f)
        buffer.applyGain(master);

    float peak = 0.0f;
    for (int channel = 0; channel < buffer.getNumChannels(); ++channel)
        peak = juce::jmax(peak, buffer.getMagnitude(channel, 0, numSamples));
    if (peak > masterPeakValue.load())
        masterPeakValue = peak;
}

juce::String MixEngine::deviceName() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getName();
    return {};
}

juce::String MixEngine::deviceTypeName() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getTypeName();
    return {};
}

int MixEngine::xrunCount() const
{
    return deviceManager.getXRunCount();
}

int MixEngine::outputLatencySamples() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getOutputLatencyInSamples();
    return 0;
}

juce::Array<double> MixEngine::availableSampleRates() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getAvailableSampleRates();
    return {};
}

juce::Array<int> MixEngine::availableBufferSizes() const
{
    if (auto* device = deviceManager.getCurrentAudioDevice())
        return device->getAvailableBufferSizes();
    return {};
}
} // namespace tsov
