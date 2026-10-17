#pragma once

#include <juce_audio_processors/juce_audio_processors.h>

#include <vector>

namespace tsov
{
class PluginHost
{
public:
    struct Entry
    {
        juce::String name;
        juce::String manufacturer;
        juce::String path;
        juce::String format;
        juce::String identifier;
    };

    static juce::StringArray defaultSearchPaths();

    // Scans VST3 bundles below the given paths. Returns an empty vector on failure
    // and fills error.
    static std::vector<Entry> scan(const juce::StringArray& searchPaths, juce::String& error);

    // Loads the first VST3 class in the given bundle and renders a short note.
    // Returns a human-readable report; error is set on failure.
    static juce::String test(const juce::String& path,
                             double sampleRate,
                             int blockSize,
                             juce::String& error);

    // Creates the first VST3 class in the given bundle (caller owns it).
    static std::unique_ptr<juce::AudioPluginInstance> create(const juce::String& path,
                                                             double sampleRate,
                                                             int blockSize,
                                                             juce::String& error);
};
} // namespace tsov
