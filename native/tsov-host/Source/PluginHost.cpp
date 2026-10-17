#include "PluginHost.h"

#include <cmath>

namespace tsov
{
namespace
{
juce::AudioPluginFormat* findVst3(juce::AudioPluginFormatManager& formats)
{
    for (int i = 0; i < formats.getNumFormats(); ++i)
        if (formats.getFormat(i)->getName().equalsIgnoreCase("VST3"))
            return formats.getFormat(i);
    return nullptr;
}

std::unique_ptr<juce::AudioPluginInstance> loadFirstClass(juce::AudioPluginFormatManager& formats,
                                                           const juce::String& path,
                                                           double sampleRate,
                                                           int blockSize,
                                                           juce::String& error)
{
    auto* vst3 = findVst3(formats);
    if (vst3 == nullptr)
    {
        error = "VST3 format not available";
        return {};
    }

    juce::OwnedArray<juce::PluginDescription> descriptions;
    vst3->findAllTypesForFile(descriptions, path);
    if (descriptions.isEmpty())
    {
        error = "no VST3 class found in " + path;
        return {};
    }

    return formats.createPluginInstance(*descriptions[0], sampleRate, blockSize, error);
}
} // namespace

juce::StringArray PluginHost::defaultSearchPaths()
{
    juce::StringArray paths;
    paths.add("C:/Program Files/Common Files/VST3");
    paths.add("D:/VST3");
    return paths;
}

std::vector<PluginHost::Entry> PluginHost::scan(const juce::StringArray& searchPaths, juce::String& error)
{
    juce::AudioPluginFormatManager formats;
    formats.addDefaultFormats();
    auto* vst3 = findVst3(formats);
    if (vst3 == nullptr)
    {
        error = "VST3 format not available";
        return {};
    }

    juce::FileSearchPath searchPath;
    for (const auto& path : searchPaths)
        searchPath.add(juce::File(path));

    juce::KnownPluginList known;
    juce::File deadMansPedal = juce::File::getSpecialLocation(juce::File::tempDirectory)
                                   .getChildFile("tsov-host-plugin-scan.txt");
    juce::PluginDirectoryScanner scanner(known, *vst3, searchPath, true, deadMansPedal, true);

    juce::String scanning;
    while (scanner.scanNextFile(true, scanning))
    {
    }

    std::vector<Entry> entries;
    for (const auto& desc : known.getTypes())
    {
        Entry entry;
        entry.name = desc.name;
        entry.manufacturer = desc.manufacturerName;
        entry.path = desc.fileOrIdentifier;
        entry.format = desc.pluginFormatName;
        entry.identifier = desc.createIdentifierString();
        entries.push_back(entry);
    }
    return entries;
}

juce::String PluginHost::test(const juce::String& path,
                              double sampleRate,
                              int blockSize,
                              juce::String& error)
{
    juce::AudioPluginFormatManager formats;
    formats.addDefaultFormats();
    auto instance = loadFirstClass(formats, path, sampleRate, blockSize, error);
    if (instance == nullptr)
        return {};

    instance->enableAllBuses();
    instance->prepareToPlay(sampleRate, blockSize);

    const int totalBlocks = (int) std::lround(sampleRate * 1.0 / blockSize);
    const int noteOffBlock = totalBlocks / 2;
    juce::AudioBuffer<float> buffer(2, blockSize);
    float peak = 0.0f;
    int nonZeroBlocks = 0;

    for (int block = 0; block < totalBlocks; ++block)
    {
        buffer.clear();
        juce::MidiBuffer midi;
        if (block == 0)
            midi.addEvent(juce::MidiMessage::noteOn(1, 60, 0.8f), 0);
        if (block == noteOffBlock)
            midi.addEvent(juce::MidiMessage::noteOff(1, 60), 0);

        instance->processBlock(buffer, midi);
        const auto blockPeak = buffer.getMagnitude(0, buffer.getNumSamples());
        peak = juce::jmax(peak, blockPeak);
        if (blockPeak > 0.000001f)
            ++nonZeroBlocks;
    }

    instance->releaseResources();

    juce::String report;
    report << "plugin=" << instance->getName() << "\n"
           << "path=" << path << "\n"
           << "sample_rate=" << sampleRate << "\n"
           << "block_size=" << blockSize << "\n"
           << "blocks=" << totalBlocks << "\n"
           << "output_channels=" << instance->getTotalNumOutputChannels() << "\n"
           << "parameters=" << instance->getParameters().size() << "\n"
           << "peak=" << juce::String(peak, 6) << "\n"
           << "non_zero_blocks=" << nonZeroBlocks << "\n"
           << "plugin_test=ok";
    return report;
}

std::unique_ptr<juce::AudioPluginInstance> PluginHost::create(const juce::String& path,
                                                              double sampleRate,
                                                              int blockSize,
                                                              juce::String& error)
{
    juce::AudioPluginFormatManager formats;
    formats.addDefaultFormats();
    return loadFirstClass(formats, path, sampleRate, blockSize, error);
}

bool PluginHost::loadState(juce::AudioPluginInstance& instance,
                           const juce::File& file,
                           juce::String& error)
{
    if (! file.existsAsFile())
    {
        error = "state file not found: " + file.getFullPathName();
        return false;
    }

    juce::MemoryBlock data;
    if (! file.loadFileAsData(data) || data.getSize() == 0)
    {
        error = "cannot read state file: " + file.getFullPathName();
        return false;
    }

    instance.setStateInformation(data.getData(), (int) data.getSize());
    return true;
}

bool PluginHost::saveState(juce::AudioPluginInstance& instance,
                           const juce::File& file,
                           juce::String& error)
{
    file.getParentDirectory().createDirectory();
    juce::MemoryBlock data;
    instance.getStateInformation(data);
    if (data.getSize() == 0)
    {
        error = "plugin returned empty state";
        return false;
    }

    if (! file.replaceWithData(data.getData(), data.getSize()))
    {
        error = "cannot write state file: " + file.getFullPathName();
        return false;
    }
    return true;
}
} // namespace tsov
