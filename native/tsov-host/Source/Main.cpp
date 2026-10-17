#include "AudioEngine.h"
#include "MixEngine.h"
#include "PluginHost.h"

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_audio_utils/juce_audio_utils.h>

#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace
{
void line(const juce::String& text)
{
    std::cout << text << std::endl;
}

struct MixTrackSpec
{
    juce::String name;
    juce::String plugin;
    juce::String state;
    double gain = 0.55;
    double pan = 0.0;
    int note = 60;
};

juce::File resolveMixPath(const juce::File& base, const juce::String& path, bool fileOnly)
{
    if (path.isEmpty())
        return {};
    juce::File direct(path);
    const auto exists = fileOnly ? direct.existsAsFile() : direct.exists();
    if (juce::File::isAbsolutePath(path) || exists)
        return direct;
    auto relative = base.getChildFile(path);
    if (fileOnly ? relative.existsAsFile() : relative.exists())
        return relative;
    return direct;
}

bool loadMixManifest(const juce::File& file,
                     std::vector<MixTrackSpec>& specs,
                     juce::String& error)
{
    if (! file.existsAsFile())
    {
        error = "manifest not found: " + file.getFullPathName();
        return false;
    }

    const auto parsed = juce::JSON::parse(file.loadFileAsString());
    auto* array = parsed.getArray();
    if (array == nullptr)
    {
        error = "manifest must be a JSON array";
        return false;
    }

    const auto base = file.getParentDirectory();
    int index = 0;
    for (const auto& item : *array)
    {
        auto* object = item.getDynamicObject();
        if (object == nullptr)
        {
            error = "manifest item " + juce::String(index) + " is not an object";
            return false;
        }

        MixTrackSpec spec;
        spec.plugin = resolveMixPath(base, object->getProperty("plugin").toString(), false).getFullPathName();
        if (spec.plugin.isEmpty())
        {
            error = "manifest item " + juce::String(index) + " has no plugin";
            return false;
        }
        spec.name = object->getProperty("name").toString();
        if (spec.name.isEmpty())
            spec.name = "track-" + juce::String(index + 1);
        spec.state = resolveMixPath(base, object->getProperty("state").toString(), true).getFullPathName();
        if (object->hasProperty("gain"))
            spec.gain = (double) object->getProperty("gain");
        if (object->hasProperty("pan"))
            spec.pan = (double) object->getProperty("pan");
        if (object->hasProperty("note"))
            spec.note = (int) object->getProperty("note");
        spec.gain = juce::jlimit(0.0, 2.0, spec.gain);
        spec.pan = juce::jlimit(-1.0, 1.0, spec.pan);
        spec.note = juce::jlimit(0, 127, spec.note);
        specs.push_back(std::move(spec));
        ++index;
    }

    if (specs.empty())
    {
        error = "manifest has no tracks";
        return false;
    }
    return true;
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
    line("  tsov-host --plugin-live <path.vst3> [--device <name|null>] [--duration <sec>] [--note <midi>] [--state <file>]");
    line("  tsov-host --plugin-editor <path.vst3> [--device <name>] [--duration <sec>] [--note <midi>] [--state <file>]");
    line("  tsov-host --mix-test <path.vst3> [--tracks <n>] [--mix-gain <g>] [--device <name|null>] [--monitor] [--record <out.wav>]");
    line("  tsov-host --mix-manifest <tracks.json> [--device <name|null>] [--duration <sec>]");
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
              int blockSize,
              int testNote,
              const juce::String& statePath)
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
    config.testNote = testNote;

    if (pluginPath.isNotEmpty())
    {
        juce::String pluginError;
        auto instance = tsov::PluginHost::create(pluginPath, sampleRate, blockSize, pluginError);
        if (instance == nullptr)
        {
            std::cerr << "plugin load failed: " << pluginError << std::endl;
            return 4;
        }
        if (statePath.isNotEmpty())
        {
            juce::String stateError;
            if (! tsov::PluginHost::loadState(*instance, juce::File(statePath), stateError))
                std::cerr << "state load failed: " << stateError << std::endl;
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
                    int blockSize,
                    int testNote,
                    const juce::String& statePath)
{
    juce::ScopedJuceInitialiser_GUI juceInit;

    juce::String pluginError;
    auto instance = tsov::PluginHost::create(pluginPath, sampleRate, blockSize, pluginError);
    if (instance == nullptr)
    {
        std::cerr << "plugin load failed: " << pluginError << std::endl;
        return 4;
    }

    if (statePath.isNotEmpty())
    {
        juce::String stateError;
        if (! tsov::PluginHost::loadState(*instance, juce::File(statePath), stateError))
            std::cerr << "state load failed: " << stateError << std::endl;
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
    config.testNote = testNote;
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

    int64_t frames = 0;
    int callbacks = 0;
    int xruns = 0;
    int latency = 0;
    float peak = 0.0f;
    juce::String pluginName;
    juce::String deviceName;
    double actualRate = 0.0;
    int actualBlock = 0;
    bool stateSaved = false;

    // Tear the editor/plugin down while the message loop is still running:
    // Kontakt 8's destructor needs the message thread; doing it after
    // stopDispatchLoop() hangs the process (P78).
    juce::Timer::callAfterDelay((int) std::lround(durationSeconds * 1000.0), [&]
    {
        window.setVisible(false);
        window.clearContentComponent();

        frames = engine.framesRendered();
        callbacks = engine.callbackCount();
        xruns = engine.xrunCount();
        latency = engine.outputLatencySamples();
        peak = engine.pluginPeak();
        pluginName = engine.pluginName();
        deviceName = engine.deviceName();
        actualRate = engine.sampleRate();
        actualBlock = engine.blockSize();

        if (statePath.isNotEmpty())
        {
            auto* plugin = engine.pluginInstance();
            if (plugin != nullptr)
            {
                juce::String stateError;
                stateSaved = tsov::PluginHost::saveState(*plugin, juce::File(statePath), stateError);
                if (! stateSaved)
                    std::cerr << "state save failed: " << stateError << std::endl;
            }
        }

        engine.close();
        engine.setPlugin(nullptr);
        juce::MessageManager::getInstance()->stopDispatchLoop();
    });

    juce::MessageManager::getInstance()->runDispatchLoop();

    if (window.getContentComponent() != nullptr)
    {
        window.setVisible(false);
        window.clearContentComponent();
    }

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
    if (statePath.isNotEmpty())
    {
        line("state_path=" + statePath);
        line("state_saved=" + juce::String(stateSaved ? 1 : 0));
    }
    line("editor=ok");
    std::cout.flush();
    // Kontakt 8 blocks in ExitProcess/DLL detach after the editor closes.
    // The device and plugin are already released above; terminate the process
    // without running DLL detach so --plugin-editor can be scripted (P78).
#if defined(_WIN32)
    ::TerminateProcess(::GetCurrentProcess(), 0);
#else
    std::_Exit(0);
#endif
    return 0;
}

int runMixEngine(bool nullDevice,
                 const juce::String& deviceSubstring,
                 const juce::String& deviceTypeSubstring,
                 const juce::String& pluginPath,
                 const juce::String& manifestPath,
                 double durationSeconds,
                 double sampleRate,
                 int blockSize,
                 int requestedTracks,
                 bool monitorInput,
                 const juce::String& recordPath,
                 double trackGain)
{
    juce::ScopedJuceInitialiser_GUI juceInit;

    tsov::MixEngine engine;
    tsov::MixEngine::Config config;
    config.deviceSubstring = deviceSubstring;
    config.deviceTypeSubstring = deviceTypeSubstring;
    config.sampleRate = sampleRate;
    config.blockSize = blockSize;
    config.inputChannels = (monitorInput || recordPath.isNotEmpty()) ? 2 : 0;
    config.monitorInput = monitorInput;

    const auto trackCount = juce::jlimit(1, 16, requestedTracks);

    std::vector<MixTrackSpec> specs;
    if (manifestPath.isNotEmpty())
    {
        juce::String manifestError;
        if (! loadMixManifest(juce::File(manifestPath), specs, manifestError))
        {
            std::cerr << "manifest load failed: " << manifestError << std::endl;
            return 6;
        }
        if (specs.size() > 16)
        {
            std::cerr << "manifest has too many tracks (max 16)" << std::endl;
            return 6;
        }
    }
    else
    {
        for (int i = 0; i < trackCount; ++i)
        {
            MixTrackSpec spec;
            spec.name = "track-" + juce::String(i + 1);
            spec.plugin = pluginPath;
            spec.gain = juce::jlimit(0.0, 2.0, trackGain);
            spec.pan = i == 0 ? -0.35 : (i == 1 ? 0.35 : 0.0);
            spec.note = 60 + i * 4;
            specs.push_back(std::move(spec));
        }
    }

    for (size_t i = 0; i < specs.size(); ++i)
    {
        juce::String pluginError;
        auto instance = tsov::PluginHost::create(specs[i].plugin, sampleRate, blockSize, pluginError);
        if (instance == nullptr)
        {
            std::cerr << "track " << i << " plugin load failed: " << pluginError << std::endl;
            return 4;
        }
        if (specs[i].state.isNotEmpty())
        {
            juce::String stateError;
            if (! tsov::PluginHost::loadState(*instance, juce::File(specs[i].state), stateError))
                std::cerr << "track " << i << " state load failed: " << stateError << std::endl;
        }
        if (engine.addTrack(std::move(instance), specs[i].name, (float) specs[i].gain, (float) specs[i].pan, specs[i].note - 60, true) < 0)
        {
            std::cerr << "addTrack failed at " << i << std::endl;
            return 5;
        }
    }
    const auto actualTrackCount = (int) specs.size();
    engine.play();

    if (nullDevice)
    {
        const auto blocks = (int) std::lround(durationSeconds * sampleRate / blockSize);
        engine.runNull(blocks);
        line("mode=mix-null");
        line("tracks=" + juce::String(engine.trackCount()));
        line("frames=" + juce::String((juce::int64) engine.framesRendered()));
        line("callbacks=" + juce::String(engine.callbackCount()));
        line("master_peak=" + juce::String(engine.masterPeak(), 6));
        for (int i = 0; i < engine.trackCount(); ++i)
        {
            const auto info = engine.trackInfo(i);
            line("track[" + juce::String(i) + "].name=" + info.name);
            line("track[" + juce::String(i) + "].notes=" + juce::String(info.noteCount));
            line("track[" + juce::String(i) + "].peak=" + juce::String(engine.trackPeak(i), 6));
        }
        line("mix=ok");
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

    juce::String recordError;
    if (recordPath.isNotEmpty())
    {
        if (! engine.startRecording(juce::File(recordPath), 2, recordError))
            std::cerr << "record start failed: " << recordError << std::endl;
    }

    const auto actualRate = engine.sampleRate();
    const auto actualBlock = engine.blockSize();
    const auto name = engine.deviceName();
    const auto typeName = engine.deviceTypeName();
    const auto latency = engine.outputLatencySamples();
    juce::Thread::sleep((int) std::lround(durationSeconds * 1000.0));

    const auto frames = engine.framesRendered();
    const auto callbacks = engine.callbackCount();
    const auto xruns = engine.xrunCount();
    const auto masterPeak = engine.masterPeak();
    const auto inputPeak = engine.inputPeak();
    const auto playhead = engine.playheadSamples();
    const auto recordedFrames = engine.recordedFrames();
    const auto recordDropouts = engine.recordingDropouts();
    engine.stopRecording();
    engine.close();
    const auto recordBytes = recordPath.isNotEmpty() ? juce::File(recordPath).getSize() : (juce::int64) 0;

    line("mode=mix");
    line("device=" + name);
    line("device_type=" + typeName);
    line("sample_rate=" + juce::String(actualRate));
    line("block_size=" + juce::String(actualBlock));
    line("tracks=" + juce::String(actualTrackCount));
    line("frames=" + juce::String((juce::int64) frames));
    line("callbacks=" + juce::String(callbacks));
    line("xruns=" + juce::String(xruns));
    line("output_latency_samples=" + juce::String(latency));
    line("playhead_samples=" + juce::String((juce::int64) playhead));
    line("input_peak=" + juce::String(inputPeak, 6));
    line("master_peak=" + juce::String(masterPeak, 6));
    if (recordPath.isNotEmpty())
    {
        line("record_path=" + recordPath);
        line("record_frames=" + juce::String((juce::int64) recordedFrames));
        line("record_dropouts=" + juce::String(recordDropouts));
        line("record_bytes=" + juce::String((juce::int64) recordBytes));
    }
    for (int i = 0; i < actualTrackCount; ++i)
    {
        line("track[" + juce::String(i) + "].name=" + engine.trackInfo(i).name);
        line("track[" + juce::String(i) + "].peak=" + juce::String(engine.trackPeak(i), 6));
    }
    line("mix=ok");
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
    juce::String mixTestPath;
    juce::String mixManifestPath;
    juce::String recordPath;
    juce::String statePath;
    int mixTracks = 2;
    double mixGain = 0.55;
    bool monitorInput = false;
    juce::String deviceSubstring;
    juce::String deviceTypeSubstring;
    double duration = 2.0;
    double toneHz = 0.0;
    double gain = 0.02;
    double sampleRate = 48000.0;
    int blockSize = 128;
    int testNote = 60;

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
        else if (arg == "--mix-test" && i + 1 < argc)
            mixTestPath = juce::String(argv[++i]);
        else if (arg == "--mix-manifest" && i + 1 < argc) mixManifestPath = juce::String(argv[++i]);
        else if (arg == "--record" && i + 1 < argc) recordPath = juce::String(argv[++i]);
        else if (arg == "--state" && i + 1 < argc) statePath = juce::String(argv[++i]);
        else if (arg == "--tracks" && i + 1 < argc) mixTracks = std::stoi(argv[++i]);
        else if (arg == "--mix-gain" && i + 1 < argc) mixGain = std::stod(argv[++i]);
        else if (arg == "--monitor") monitorInput = true;
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
        else if (arg == "--note" && i + 1 < argc) testNote = std::stoi(argv[++i]);
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
        return runPluginEditor(pluginEditorPath, deviceSubstring, deviceTypeSubstring, duration, sampleRate, blockSize, testNote, statePath);
    if (mixTestPath.isNotEmpty() || mixManifestPath.isNotEmpty())
        return runMixEngine(nullDevice, deviceSubstring, deviceTypeSubstring, mixTestPath, mixManifestPath, duration, sampleRate, blockSize, mixTracks, monitorInput, recordPath, mixGain);
    if (run)
        return runEngine(nullDevice, deviceSubstring, deviceTypeSubstring, pluginLivePath, duration, toneHz, gain, sampleRate, blockSize, testNote, statePath);

    printUsage();
    return 2;
}
