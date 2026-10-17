#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_audio_utils/juce_audio_utils.h>

#include <iostream>
#include <string>

namespace
{
void line(const juce::String& text)
{
    std::cout << text << std::endl;
}

juce::String juceVersion()
{
    return juce::String(JUCE_MAJOR_VERSION) + "."
         + juce::String(JUCE_MINOR_VERSION) + "."
         + juce::String(JUCE_BUILDNUMBER);
}

int runSelfTest(bool nullDevice)
{
    juce::ScopedJuceInitialiser_GUI juceInit;

    line("tsov-host selftest");
    line("juce=" + juceVersion());
    line("application=tsov-host 0.1.0");
    line(juce::String("null_device=") + (nullDevice ? "1" : "0"));

    juce::AudioDeviceManager deviceManager;
    auto& types = deviceManager.getAvailableDeviceTypes();
    line("audio_device_types=" + juce::String(types.size()));
    for (int i = 0; i < types.size(); ++i)
        line("  device_type[" + juce::String(i) + "]=" + types[i]->getTypeName());

    juce::AudioPluginFormatManager formats;
    formats.addDefaultFormats();
    line("plugin_formats=" + juce::String(formats.getNumFormats()));
    for (int i = 0; i < formats.getNumFormats(); ++i)
        line("  plugin_format[" + juce::String(i) + "]=" + formats.getFormat(i)->getName());

    line("selftest=ok");
    return 0;
}

void printUsage()
{
    line("tsov-host 0.1.0 (JUCE " + juceVersion() + ")");
    line("usage:");
    line("  tsov-host --selftest [--device null]");
    line("  tsov-host --list-devices");
    line("  tsov-host --version");
    line("  tsov-host --help");
}

int listDevices()
{
    juce::ScopedJuceInitialiser_GUI juceInit;
    juce::AudioDeviceManager deviceManager;
    auto& types = deviceManager.getAvailableDeviceTypes();

    for (int i = 0; i < types.size(); ++i)
    {
        types[i]->scanForDevices();
        line("[" + types[i]->getTypeName() + "]");
        const auto outputs = types[i]->getDeviceNames(false);
        for (const auto& name : outputs)
            line("  out: " + name);
        const auto inputs = types[i]->getDeviceNames(true);
        for (const auto& name : inputs)
            line("  in:  " + name);
    }
    return 0;
}
} // namespace

int main(int argc, char** argv)
{
    bool selfTest = false;
    bool nullDevice = false;
    bool devices = false;

    for (int i = 1; i < argc; ++i)
    {
        const std::string arg(argv[i]);
        if (arg == "--selftest") selfTest = true;
        else if (arg == "--list-devices") devices = true;
        else if (arg == "--device" && i + 1 < argc)
        {
            const std::string value(argv[++i]);
            if (value == "null") nullDevice = true;
            else
            {
                std::cerr << "unknown device: " << value << std::endl;
                return 2;
            }
        }
        else if (arg == "--version")
        {
            line("tsov-host 0.1.0 (JUCE " + juceVersion() + ")");
            return 0;
        }
        else if (arg == "--help" || arg == "-h")
        {
            printUsage();
            return 0;
        }
        else
        {
            std::cerr << "unknown argument: " << arg << std::endl;
            printUsage();
            return 2;
        }
    }

    if (selfTest)
        return runSelfTest(nullDevice);
    if (devices)
        return listDevices();

    printUsage();
    return 2;
}
