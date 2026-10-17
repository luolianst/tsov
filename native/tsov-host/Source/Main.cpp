#include "AudioEngine.h"
#include "PluginHost.h"

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_audio_utils/juce_audio_utils.h>

#include <cmath>
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
    line("  tsov-host --run [--device <name|null>] [--duration <sec>] [--tone <Hz>] [--gain <0..1>]");
    line("                   [--sample-rate <Hz>] [--block-size <frames>]");
    line("  tsov-host --scan-vst3");
    line("  tsov-host --plugin-test <path.vst3> [--sample-rate <Hz>] [--block-size <frames>]");
    line("  tsov-host --plugin-live <path.vst3> [--device <name|null>] [--duration <sec>]");
    line("  tsov-host --plugin-editor <path.vst3> [--device <name>] [--duration <sec>]");
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

int runEngine(bool nullDevice,
              const juce::String& deviceSubstring,
              const juce::String& deviceTypeSubstring,
              const juce::String& pluginPath,
              double durationSeconds,
              double toneHz,
              double gain,
              double sampleRate,
              int blockSize)
{
    juce::ScopedJuceInitialiser_GUI juceInit;

    tsov::AudioEngine engine;
    tsov::AudioEngine::Config config;
    config.deviceSubstring = deviceSubstring;
    config.deviceTypeSubstring = deviceTypeSubstring;
    config.sampleRate = sampleRate;
    config.blockSize = blockSize;
    config.toneHz = toneHz;
    config.gain = gain;

    if (pluginPath.isNotEmpty())
    {
        juce::String pluginError;
        auto instance = tsov::PluginHost::create(pluginPath, sampleRate, blockSize, pluginError);
        if (instance == nullptr)
        {
            std::cerr << "plugin load failed: " << pluginError << std::endl;
            return 4;
        }
        engine.setPlugin(std::move(instance));
    }

    if (nullDevice)
    {
        const auto blocks = (int) std::lround(durationSeconds * sampleRate / blockSize);
        const auto start = juce::Time::getMillisecondCounterHiRes();
        engine.runNull(blocks);
        const auto elapsed = juce::Time::getMillisecondCounterHiRes() - start;
        line("mode=null");
        line("sample_rate=" + juce::String(sampleRate));
        line("block_size=" + juce::String(blockSize));
        line("blocks=" + juce::String(blocks));
        line("frames=" + juce::String((juce::int64) engine.framesRendered()));
        line("callbacks=" + juce::String(engine.callbackCount()));
        line("elapsed_ms=" + juce::String(elapsed, 1));
        line("tone_hz=" + juce::String(toneHz));
        line("plugin=" + engine.pluginName());
        line("plugin_peak=" + juce::String(engine.pluginPeak(), 6));
        line("run=ok");
        return 0;
    }

    juce::String error;
    if (! engine.open(config, error))
    {
        std::cerr << "open failed: " << error << std::endl;
        return 3;
    }

    if (error.isNotEmpty())
        std::cerr << "warning: " << error << std::endl;

    const auto sampleRateActual = engine.sampleRate();
    const auto blockSizeActual = engine.blockSize();
    const auto sleepMs = (int) std::lround(durationSeconds * 1000.0);
    juce::Thread::sleep(sleepMs);

    const auto frames = engine.framesRendered();
    const auto callbacks = engine.callbackCount();
    const auto xruns = engine.xrunCount();
    const auto latency = engine.outputLatencySamples();
    juce::String rates;
    for (auto rate : engine.availableSampleRates())
        rates << (rates.isEmpty() ? "" : ",") << rate;
    juce::String buffers;
    for (auto size : engine.availableBufferSizes())
        buffers << (buffers.isEmpty() ? "" : ",") << size;
    const auto name = engine.deviceName();
    const auto typeName = engine.deviceTypeName();
    engine.close();

    line("mode=live");
    line("device=" + name);
    line("device_type=" + typeName);
    line("sample_rate=" + juce::String(sampleRateActual));
    line("block_size=" + juce::String(blockSizeActual));
    line("available_sample_rates=" + rates);
    line("available_buffer_sizes=" + buffers);
    line("frames=" + juce::String((juce::int64) frames));
    line("callbacks=" + juce::String(callbacks));
    line("xruns=" + juce::String(xruns));
    line("output_latency_samples=" + juce::String(latency));
    line("tone_hz=" + juce::String(toneHz));
    line("plugin=" + engine.pluginName());
    line("plugin_peak=" + juce::String(engine.pluginPeak(), 6));
    line("run=ok");
    return 0;
}

int scanVst3()
{
    juce::ScopedJuceInitialiser_GUI juceInit;
    juce::String error;
    const auto entries = tsov::PluginHost::scan(tsov::PluginHost::defaultSearchPaths(), error);
    if (error.isNotEmpty())
    {
        std::cerr << "scan failed: " << error << std::endl;
        return 3;
    }

    line("vst3_count=" + juce::String((int) entries.size()));
    for (const auto& entry : entries)
    {
        line("plugin=" + entry.name);
        line("  manufacturer=" + entry.manufacturer);
        line("  path=" + entry.path);
        line("  format=" + entry.format);
        line("  id=" + entry.identifier);
    }
    line("scan=ok");
    return 0;
}

int testPlugin(const juce::String& path, double sampleRate, int blockSize)
{
    juce::ScopedJuceInitialiser_GUI juceInit;
    juce::String error;
    const auto report = tsov::PluginHost::test(path, sampleRate, blockSize, error);
    if (error.isNotEmpty())
    {
        std::cerr << "plugin test failed: " << error << std::endl;
        return 3;
    }
    if (report.isEmpty())
    {
        std::cerr << "plugin test failed: empty report" << std::endl;
        return 3;
    }

    line(report);
    return 0;
}

int runPluginEditor(const juce::String& pluginPath,
                    const juce::String& deviceSubstring,
                    const juce::String& deviceTypeSubstring,
                    double durationSeconds,
                    double sampleRate,
                    int blockSize)
{
    juce::ScopedJuceInitialiser_GUI juceInit;

    juce::String pluginError;
    auto instance = tsov::PluginHost::create(pluginPath, sampleRate, blockSize, pluginError);
    if (instance == nullptr)
    {
        std::cerr << "plugin load failed: " << pluginError << std::endl;
        return 4;
    }

    auto* editor = instance->createEditorIfNeeded();
    if (editor == nullptr)
    {
        std::cerr << "plugin has no editor: " << instance->getName() << std::endl;
        return 5;
    }

    tsov::AudioEngine engine;
    tsov::AudioEngine::Config config;
    config.deviceSubstring = deviceSubstring;
    config.deviceTypeSubstring = deviceTypeSubstring;
    config.sampleRate = sampleRate;
    config.blockSize = blockSize;
    config.toneHz = 0.0;
    engine.setPlugin(std::move(instance));

    juce::String error;
    if (! engine.open(config, error))
    {
        std::cerr << "open failed: " << error << std::endl;
        return 3;
    }

    juce::DocumentWindow window("tsov-host · " + engine.pluginName(),
                                juce::Colours::darkgrey,
                                juce::DocumentWindow::allButtons);
    window.setUsingNativeTitleBar(true);
    window.setContentOwned(editor, true);
    window.setResizable(true, false);
    window.centreWithSize(juce::jmax(480, editor->getWidth()), juce::jmax(320, editor->getHeight()));
    window.setVisible(true);
    window.toFront(true);

    juce::MessageManager::getInstance()->runDispatchLoopUntil((int) std::lround(durationSeconds * 1000.0));

    const auto frames = engine.framesRendered();
    const auto callbacks = engine.callbackCount();
    const auto xruns = engine.xrunCount();
    const auto latency = engine.outputLatencySamples();
    const auto peak = engine.pluginPeak();
    const auto pluginName = engine.pluginName();
    const auto deviceName = engine.deviceName();
    const auto actualRate = engine.sampleRate();
    const auto actualBlock = engine.blockSize();
    engine.close();
    window.setVisible(false);
    window.clearContentComponent();

    line("mode=plugin-editor");
    line("plugin=" + pluginName);
    line("device=" + deviceName);
    line("sample_rate=" + juce::String(actualRate));
    line("block_size=" + juce::String(actualBlock));
    line("frames=" + juce::String((juce::int64) frames));
    line("callbacks=" + juce::String(callbacks));
    line("xruns=" + juce::String(xruns));
    line("output_latency_samples=" + juce::String(latency));
    line("plugin_peak=" + juce::String(peak, 6));
    line("editor=ok");
    return 0;
}
} // namespace

int main(int argc, char** argv)
{
    bool selfTest = false;
    bool nullDevice = false;
    bool devices = false;
    bool run = false;
    bool scanVst3Flag = false;
    juce::String pluginTestPath;
    juce::String pluginLivePath;
    juce::String pluginEditorPath;
    juce::String deviceSubstring;
    juce::String deviceTypeSubstring;
    double duration = 2.0;
    double toneHz = 0.0;
    double gain = 0.02;
    double sampleRate = 48000.0;
    int blockSize = 128;

    for (int i = 1; i < argc; ++i)
    {
        const std::string arg(argv[i]);
        if (arg == "--selftest") selfTest = true;
        else if (arg == "--list-devices") devices = true;
        else if (arg == "--run") run = true;
        else if (arg == "--scan-vst3") scanVst3Flag = true;
        else if (arg == "--plugin-test" && i + 1 < argc) pluginTestPath = juce::String(argv[++i]);
        else if (arg == "--plugin-live" && i + 1 < argc)
        {
            pluginLivePath = juce::String(argv[++i]);
            run = true;
        }
        else if (arg == "--plugin-editor" && i + 1 < argc)
            pluginEditorPath = juce::String(argv[++i]);
        else if (arg == "--device" && i + 1 < argc)
        {
            const std::string value(argv[++i]);
            if (value == "null") nullDevice = true;
            else deviceSubstring = juce::String(value);
        }
        else if (arg == "--device-type" && i + 1 < argc) deviceTypeSubstring = juce::String(argv[++i]);
        else if (arg == "--duration" && i + 1 < argc) duration = std::stod(argv[++i]);
        else if (arg == "--tone" && i + 1 < argc) toneHz = std::stod(argv[++i]);
        else if (arg == "--gain" && i + 1 < argc) gain = std::stod(argv[++i]);
        else if (arg == "--sample-rate" && i + 1 < argc) sampleRate = std::stod(argv[++i]);
        else if (arg == "--block-size" && i + 1 < argc) blockSize = std::stoi(argv[++i]);
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

    if (duration <= 0.0 || sampleRate <= 0.0 || blockSize <= 0)
    {
        std::cerr << "duration/sample-rate/block-size must be positive" << std::endl;
        return 2;
    }

    if (selfTest)
        return runSelfTest(nullDevice);
    if (devices)
        return listDevices();
    if (scanVst3Flag)
        return scanVst3();
    if (pluginTestPath.isNotEmpty())
        return testPlugin(pluginTestPath, sampleRate, blockSize);
    if (pluginEditorPath.isNotEmpty())
        return runPluginEditor(pluginEditorPath, deviceSubstring, deviceTypeSubstring, duration, sampleRate, blockSize);
    if (run)
        return runEngine(nullDevice, deviceSubstring, deviceTypeSubstring, pluginLivePath, duration, toneHz, gain, sampleRate, blockSize);

    printUsage();
    return 2;
}
