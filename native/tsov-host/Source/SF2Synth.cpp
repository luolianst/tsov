#include "SF2Synth.h"

#include <cmath>
#include <algorithm>

#if JUCE_WINDOWS
 #include <windows.h>
#endif

namespace tsov
{
namespace
{
using NewSettingsFn = void* (*)();
using DeleteSettingsFn = void (*)(void*);
using SetNumFn = int (*)(void*, const char*, double);
using NewSynthFn = void* (*)(void*);
using DeleteSynthFn = void (*)(void*);
using SfLoadFn = int (*)(void*, const char*, int);
using ProgramSelectFn = int (*)(void*, int, int, int, int);
using NoteOnFn = int (*)(void*, int, int, int);
using NoteOffFn = int (*)(void*, int, int);
using AllNotesOffFn = int (*)(void*, int);
using SetGainFn = int (*)(void*, float);
using WriteFloatFn = int (*)(void*, int, float*, int, int, float*, int, int);

NewSettingsFn pNewSettings = nullptr;
DeleteSettingsFn pDeleteSettings = nullptr;
SetNumFn pSetNum = nullptr;
NewSynthFn pNewSynth = nullptr;
DeleteSynthFn pDeleteSynth = nullptr;
SfLoadFn pSfLoad = nullptr;
ProgramSelectFn pProgramSelect = nullptr;
NoteOnFn pNoteOn = nullptr;
NoteOffFn pNoteOff = nullptr;
AllNotesOffFn pAllNotesOff = nullptr;
SetGainFn pSetGain = nullptr;
WriteFloatFn pWriteFloat = nullptr;

#if JUCE_WINDOWS
HMODULE fluidModule = nullptr;
#endif

template <typename Fn>
bool bindProc(Fn& fn, const char* name, juce::String& error)
{
#if JUCE_WINDOWS
    fn = reinterpret_cast<Fn>(GetProcAddress(fluidModule, name));
#else
    fn = nullptr;
    juce::ignoreUnused(name);
#endif
    if (fn == nullptr)
    {
        error = juce::String("FluidSynth symbol missing: ") + name;
        return false;
    }
    return true;
}

bool ensureLibrary(const juce::String& libraryPath, juce::String& error)
{
#if JUCE_WINDOWS
    if (fluidModule != nullptr)
        return true;

    auto path = libraryPath;
    if (path.isEmpty())
        path = "vendor/fluidsynth/bin/libfluidsynth-3.dll";
    auto file = juce::File(path);
    if (! juce::File::isAbsolutePath(path))
        file = juce::File::getCurrentWorkingDirectory().getChildFile(path);
    auto wide = file.getFullPathName().toWideCharPointer();
    fluidModule = LoadLibraryExW(wide, nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (fluidModule == nullptr)
    {
        error = "cannot load FluidSynth: " + file.getFullPathName() + " (WinError " + juce::String((int) GetLastError()) + ")";
        return false;
    }

    return bindProc(pNewSettings, "new_fluid_settings", error)
        && bindProc(pDeleteSettings, "delete_fluid_settings", error)
        && bindProc(pSetNum, "fluid_settings_setnum", error)
        && bindProc(pNewSynth, "new_fluid_synth", error)
        && bindProc(pDeleteSynth, "delete_fluid_synth", error)
        && bindProc(pSfLoad, "fluid_synth_sfload", error)
        && bindProc(pProgramSelect, "fluid_synth_program_select", error)
        && bindProc(pNoteOn, "fluid_synth_noteon", error)
        && bindProc(pNoteOff, "fluid_synth_noteoff", error)
        && bindProc(pAllNotesOff, "fluid_synth_all_notes_off", error)
        && bindProc(pSetGain, "fluid_synth_set_gain", error)
        && bindProc(pWriteFloat, "fluid_synth_write_float", error);
#else
    juce::ignoreUnused(libraryPath);
    error = "SF2 playback is currently Windows-only";
    return false;
#endif
}
} // namespace

SF2Synth::~SF2Synth()
{
    close();
}

void SF2Synth::close()
{
    if (synth != nullptr)
    {
        if (pAllNotesOff != nullptr)
            pAllNotesOff(synth, -1);
        if (pDeleteSynth != nullptr)
            pDeleteSynth(synth);
        synth = nullptr;
    }
    if (settings != nullptr)
    {
        if (pDeleteSettings != nullptr)
            pDeleteSettings(settings);
        settings = nullptr;
    }
}

bool SF2Synth::load(const juce::String& soundfont,
                    int bank,
                    int program,
                    double sampleRate,
                    const juce::String& libraryPath,
                    juce::String& error)
{
    close();
    if (! ensureLibrary(libraryPath, error))
        return false;

    soundfontPath = soundfont;
    dllPath = libraryPath;
    bankNumber = bank;
    programNumber = program;
    currentSampleRate = sampleRate;

    settings = pNewSettings();
    if (settings == nullptr)
    {
        error = "new_fluid_settings failed";
        return false;
    }
    pSetNum(settings, "synth.sample-rate", sampleRate);

    synth = pNewSynth(settings);
    if (synth == nullptr)
    {
        error = "new_fluid_synth failed";
        close();
        return false;
    }
    pSetGain(synth, 0.8f);

    const auto sfid = pSfLoad(synth, soundfont.toRawUTF8(), 1);
    if (sfid < 0)
    {
        error = "SoundFont load failed: " + soundfont;
        close();
        return false;
    }
    if (pProgramSelect(synth, 0, sfid, bank, program) != 0)
    {
        error = "program_select failed (bank/program)";
        close();
        return false;
    }
    return true;
}

bool SF2Synth::prepare(double sampleRate, juce::String& error)
{
    if (synth != nullptr && std::abs(sampleRate - currentSampleRate) < 0.5)
        return true;
    if (soundfontPath.isEmpty())
    {
        error = "SF2 source was not loaded";
        return false;
    }
    return load(soundfontPath, bankNumber, programNumber, sampleRate, dllPath, error);
}

void SF2Synth::noteOn(int note, float velocity)
{
    if (synth != nullptr)
        pNoteOn(synth, 0, juce::jlimit(0, 127, note), juce::jlimit(0, 127, (int) std::lround(velocity * 127.0f)));
}

void SF2Synth::noteOff(int note)
{
    if (synth != nullptr)
        pNoteOff(synth, 0, juce::jlimit(0, 127, note));
}

void SF2Synth::allNotesOff()
{
    if (synth != nullptr)
        pAllNotesOff(synth, -1);
}

void SF2Synth::render(float* left, float* right, int numSamples)
{
    if (synth == nullptr || numSamples <= 0)
    {
        if (left != nullptr)
            std::fill(left, left + juce::jmax(0, numSamples), 0.0f);
        if (right != nullptr)
            std::fill(right, right + juce::jmax(0, numSamples), 0.0f);
        return;
    }
    pWriteFloat(synth, numSamples, left, 0, 1, right, 0, 1);
}
} // namespace tsov
