"""Extract the melody track from a multi-track MIDI file -> single-track MIDI.

M2 reference preparation (ADR-0006): transcription backends (crepe_notes/basic-pitch)
transcribe monophonic vocal lines, so ground-truth MIDI must be a single melodic line.

Strategy:
1. Drop drum tracks (is_drum) and tracks with almost no notes.
2. Score each remaining track:
   - vocal-range coverage: % of notes within MIDI 48..84 (human vocal range)
   - monophony ratio: fraction of time with <=1 simultaneous note
   - note count (penalty for near-empty tracks)
3. Pick highest score. If tie/ambiguous, prefer track whose name hints melody
   (contains 'melody', 'lead', 'vocal', 'voice', 'solo', 'main', 'fiddle', 'flute', 'trumpet', etc.)
4. Write single-track MIDI with those notes (filtered to 48..84 optionally).

Usage: python extract-melody.py <in.mid> <out.mid> [--keep-range]
"""
import sys
import mido
from collections import defaultdict

HINT_WORDS = ('melody', 'lead', 'vocal', 'voice', 'solo', 'main', 'fiddle',
              'flute', 'trumpet', 'sax', 'oboe', 'violin', 'piccolo', 'whistle', 'tin', 'song')
VOCAL_LO, VOCAL_HI = 48, 84


def track_notes(track, ticks_per_beat, tempo=500000):
    """Return list of (start_sec, end_sec, pitch, velocity) for note_on/note_off pairs."""
    notes = []
    pending = {}  # (channel, pitch) -> (start_tick, velocity)
    cur_tick = 0
    for msg in track:
        cur_tick += msg.time
        if msg.type == 'note_on' and msg.velocity > 0:
            pending[(msg.channel, msg.note)] = (cur_tick, msg.velocity)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if key in pending:
                start_tick, vel = pending.pop(key)
                sec_per_tick = tempo / 1_000_000 / ticks_per_beat
                notes.append((start_tick * sec_per_tick, cur_tick * sec_per_tick, msg.note, vel))
    return notes


def analyze_track(track, ticks_per_beat, tempo=500000):
    notes = track_notes(track, ticks_per_beat, tempo)
    if not notes:
        return None
    name = track.name or ''
    pitches = [n[2] for n in notes]
    in_vocal = sum(1 for p in pitches if VOCAL_LO <= p <= VOCAL_HI) / len(pitches)
    # monophony ratio
    events = []
    for s, e, p, v in notes:
        events.append((s, 1))
        events.append((e, -1))
    events.sort()
    cur, maxpoly, mono_time, total_time = 0, 0, 0.0, 0.0
    prev_t = events[0][0]
    for t, d in events:
        span = t - prev_t
        if span > 0:
            if cur <= 1:
                mono_time += span
            total_time += span
            maxpoly = max(maxpoly, cur)
        cur += d
        prev_t = t
    mono_ratio = mono_time / total_time if total_time > 0 else 1.0
    hint = 1.0 if any(w in name.lower() for w in HINT_WORDS) else 0.0
    return {
        'name': name, 'program': getattr(track, 'program', None),
        'notes': len(notes), 'pitch_range': (min(pitches), max(pitches)),
        'vocal_ratio': in_vocal, 'mono_ratio': mono_ratio,
        'max_poly': maxpoly, 'hint': hint,
        'duration': (max(n[1] for n in notes) - min(n[0] for n in notes)) if notes else 0,
        'score': in_vocal * 0.4 + mono_ratio * 0.3 + hint * 0.2 + min(1, len(notes) / 300) * 0.1,
    }


def main():
    in_path, out_path = sys.argv[1], sys.argv[2]
    keep_range = '--keep-range' in sys.argv
    mid = mido.MidiFile(in_path, charset='latin1')
    ticks_per_beat = mid.ticks_per_beat
    # tempo from first meta message
    tempo = 500000
    for track in mid.tracks:
        for msg in track:
            if msg.type == 'set_tempo':
                tempo = msg.tempo
                break
        break

    candidates = []
    for i, track in enumerate(mid.tracks):
        if getattr(track, 'is_drum', False):
            continue
        info = analyze_track(track, ticks_per_beat, tempo)
        if info:
            info['idx'] = i
            candidates.append(info)

    if not candidates:
        print('NO_NONDRUM_TRACKS')
        sys.exit(1)

    candidates.sort(key=lambda x: x['score'], reverse=True)
    print('=== candidate ranking ===')
    for c in candidates[:8]:
        print(f"  [{c['idx']}] {c['name'][:40]!r} prog={c['program']} notes={c['notes']} "
              f"pitch={c['pitch_range']} vocal={c['vocal_ratio']:.2f} mono={c['mono_ratio']:.2f} "
              f"poly={c['max_poly']} hint={c['hint']:.0f} score={c['score']:.3f}")

    best = candidates[0]
    src_track = mid.tracks[best['idx']]
    notes = track_notes(src_track, ticks_per_beat, tempo)
    if not keep_range:
        notes = [n for n in notes if VOCAL_LO <= n[2] <= VOCAL_HI]

    out = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    out_track = mido.MidiTrack()
    out.tracks.append(out_track)
    out_track.append(mido.MetaMessage('track_name', name=f'melody ({best["name"]})', time=0))
    out_track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=0))
    out_track.append(mido.MetaMessage('time_signature', numerator=4, denominator=4, time=0))

    sec_per_tick = tempo / 1_000_000 / ticks_per_beat
    notes.sort(key=lambda n: n[0])
    cur_tick = 0
    for s, e, pitch, vel in notes:
        s_tick = int(s / sec_per_tick)
        e_tick = int(e / sec_per_tick)
        if s_tick < cur_tick:
            s_tick = cur_tick
        if e_tick <= s_tick:
            e_tick = s_tick + 1
        out_track.append(mido.Message('note_on', note=pitch, velocity=max(1, vel), time=s_tick - cur_tick))
        cur_tick = s_tick
        out_track.append(mido.Message('note_off', note=pitch, velocity=0, time=e_tick - s_tick))
        cur_tick = e_tick
    out.save(out_path)
    print(f"SAVED {out_path}  track='{best['name']}' notes={len(notes)} (from {best['notes']})")


if __name__ == '__main__':
    main()
