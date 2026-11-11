# Finishing: grade, captions, export and copy

16mm Journey ep1 was the first project taken all the way from raw footage to scheduled posts. It went through 10 long-form versions, 10 grade rounds and 3 vertical rebuilds. This page records what that run taught. Rules that can be data live in `config/brand_rules.yaml`, and the rest are the steps below.

## Order of work

1. **Edit the long-form:** `pace`, Tom's notes, `resolve-edit`. Every round duplicates the last timeline ("… long v10"), so Tom's own changes carry forward and any round can be reverted.
2. **Grade:** the hand grade stays on the clip nodes, and fixes go on the color group (below).
3. **Build the verticals from the latest long-form.** They use the same sections, and pick up the later trims, grade, music and caption fixes. Clip ids are `vert-<slug>` (`proposals.clip_prefix`).
4. **Run a Sonnet review on every timeline meant for posting, covering both edit and grade.** Apply only what Tom approves.
5. **Export** (below).
6. **Thumbnail.**
7. **Social copy, last**, drafted from the exported files.

## Grade

Resolve's scripting API can't move wheels or qualifiers. It can set a LUT on a color group's post-clip node, so grade fixes are LUTs:

```bash
cutroom lut ZVC_ep2_fix_a.cube                      # the default preset (talking-head-v9i)
cutroom lut ZVC_ep2_fix_b.cube --set skin_sat=0.19  # a variant to compare
```

The LUT works in DaVinci Intermediate log, the timeline's working space. A LUT built in display space barely moved anything. Install it like this:

1. Copy the `.cube` to `/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/MCP/`.
2. Call `RefreshLUTList()`.
3. Call `SetLUT(1, "MCP/<name>.cube")` on the group's post-clip node graph.

Verticals take the long-form's grade through `CopyGrades` and go in the same group, so one LUT change updates every deliverable.

Tom's rules, enforced by the loader where they can be:

| rule | why (ep1) | in config |
|---|---|---|
| Never shift his skin hue | Hue-rotated versions were rejected as "yellowed" (v9b) and "stripped out his pink" (v9c) | a preset with `hue` set is refused |
| Soften the pink, don't remove it | Skin saturation −29% still looked "sunburnt" (v9g). −19% greyed him (v9h). −22% shipped (v9i) | `skin_sat` |
| Pull back only the hottest reds | The red Sleepaway Camp poster and the sunburn hotspots needed a soft knee, not a global desaturation | `red_w`, `knee`, `knee_slope`, `red_gsat` |
| True blacks | An ungated exposure offset still greyed the blacks (v9e) | `lift` is refused; offsets fade out below `wb_lo` |
| Judge by eye on real frames | Numbers guided the work, but Tom's eye decided every round | (process) |

Verify each version with `GrabStill` and `ExportStills`. Give every still its own folder, because stills are named by timeline and clip and otherwise overwrite each other. Show Tom the last approved version, the new one and any alternative side by side. Then check one frame of the exported file too.

## Captions and subtitles

- **Every word that ends up on screen or in copy must be checked by ear.** Resolve's transcription misheard ep1 several times:
  - "I don't see how that applies to me" was really "We'll see…"
  - "complete shit piece" was really "completely chipped piece"
  - "Not bad, boy" was really "Look at that bad boy"

  One misheard line reached the SRT, the burned-in captions, the thumbnail options and the social copy.
- **Names go in the review glossary:** "Saami", "Eiki Super Slot-Load II" and "16mm".
- **A number and its unit are one caption word** (`merge_units`). The aligner returned "16" and "millimeter" separately, so a line broke between them.
- **When clips are trimmed, keep caption lines whole.** Re-check the last words of each clip. ep1 lost a "me" at a trim, which left a lone "TO" on screen.
- **Overlays need `alpha=1` and a `fontsdir` holding the font** (`render_overlays` does both). Without them the overlay renders blank.
- **The YouTube SRT comes from the long-form's final subtitle track.** Fix a misheard line in the SRT and in the overlays together.

## Titles and intros

- **A nested title must run at the timeline's frame rate.** A 29.97fps title inside a 30fps timeline returned an empty first frame of its Fusion glitch effect, which showed as a black frame. Build title timelines at the project rate, or render the title to ProRes 422 HQ and cut the file in.
- **Shorts open on `delivery.vertical_intro`** (7 frames). In Resolve, moving a V1 clip also moves its linked A1 clip. Shift each track once, then compare every track with the previous version offset by the intro. A double shift put ep1 7 frames out of sync.
- **The long-form opens on its own title card, with no glitch intro** (`delivery.wide_intro: null`). Tom prefers the titles.

## Export

- **Presets live in `formats.*.render`:**
  - Shorts: H.264 1080x1920, automatic quality.
  - Long-form: H.264 High 3840x2160, 50 Mbps, multi-pass, AAC.

  In the scripting API, call `SetCurrentRenderFormatAndCodec`, and set render settings one key at a time. A combined dict can fail silently.
- **Every final is loudness-normalised with two-pass ffmpeg loudnorm, copying the video stream.** Targets are in `delivery`: −14 LUFS, true peak −1.5, LRA 11. Use linear mode for shorts and dynamic for the long-form. Raw renders sit around −33 LUFS.
- **Raw renders go in `review-vN/`, and finals in `delivery.ready_dir`:**
  - numbered shorts
  - `<series>-ep<N> long 4K.mp4`
  - `… long 4K.en.srt`

  A re-export replaces those files in place.
- **Check each final:** frame count, loudness, and one frame at any caption that changed.

## Thumbnail and copy

- **Candidate stills come from the final export, at full 4K.** The quote on the thumbnail must be Tom's exact words, trimmed only. YouTube caps thumbnails at 2 MB, so deliver a JPG.
- **Copy follows the ZVC social copy standards:**
  - Use only Tom and Mary's exact words.
  - End with a "Changes to your wording" list.
  - Mark it as a draft.
  - Open the links.
  - Keep long-form titles to about 70 characters, with the searchable subject early.
- **Nothing posts automatically.** Tom schedules shorts in Buffer and the long-form on YouTube.
