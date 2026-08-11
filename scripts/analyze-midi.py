import sys, pretty_midi

def analyze(path):
    print(f"=== {path.split('/')[-1]} ===")
    pm = pretty_midi.PrettyMIDI(path)
    print(f"Time signature: {pm.time_signature_changes}")
    print(f"Key signature: {pm.key_signature_changes}")
    print(f"Tempo: {pm.estimate_tempo():.1f} BPM (estimated)")
    print(f"Total tracks: {len(pm.instruments)}")
    for i, inst in enumerate(pm.instruments):
        notes = inst.notes
        if not notes:
            print(f"  [{i}] {inst.name or '(unnamed)'} program={inst.program} is_drum={inst.is_drum} notes=0")
            continue
        pitches = [n.pitch for n in notes]
        starts = [n.start for n in notes]
        ends = [n.end for n in notes]
        dur = sum(n.end - n.start for n in notes)
        # polyphony: max simultaneous notes
        events = []
        for n in notes:
            events.append((n.start, 1))
            events.append((n.end, -1))
        events.sort()
        cur = 0; maxpoly = 0
        for _, d in events:
            cur += d
            maxpoly = max(maxpoly, cur)
        print(f"  [{i}] {inst.name or '(unnamed)'} program={inst.program} is_drum={inst.is_drum} notes={len(notes)} "
              f"pitch=[{min(pitches)},{max(pitches)}] time=[{min(starts):.1f},{max(ends):.1f}] "
              f"total_dur={dur:.1f}s max_poly={maxpoly}")
    # check track 0
    if pm.instruments and pm.instruments[0].notes:
        n0 = pm.instruments[0].notes[0]
        print(f"  sample note: pitch={n0.pitch} start={n0.start:.3f} end={n0.end:.3f}")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
