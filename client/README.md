# raven-image

Text-to-image for Yggdrasil Minds on Raven's ComfyUI (FLUX.3 Klein). One Python
file, standard library only, plus the ComfyUI API graph it submits.

**Value:** a Mind can make concept art with one command and no ComfyUI knowledge.
**Entry points:** `raven-image` (the client), `graphs/flux3-klein.json` (the graph).
**Live:** Klein t2i with the prompt upsampler. **Also live:** `--ref` reference-conditioned edit and `--margin` clear edges (below). **Not built:** other families
(MOSS), img2img, a custom sampler node (not needed: see `--margin`), a queue of our own, any Idunn or CultMesh surface.

```
raven-image "concept art: a lighthouse on a basalt cliff at dusk" -o out.png
raven-image "short prompt" -n 4 --size 1536x1024 --seed 7 -o out.png   # out-1.png ... out-4.png
raven-image "a full caption" --no-enhance -o out.png                  # skip the upsampler
```

Prints one line per image (`path seed size seconds`). Defaults: 1536x1024, random
seed, 900 s wait per image.

```
raven-image --ref sheet.png "Using the character from <ref_image_0>, show her seated at a desk typing, ..." --size 1024x1024 -o out.png
raven-image --ref a.png --ref b.png --vision "<ref_image_0> ... <ref_image_1> ..." -o out.png
```

`--ref` (repeatable) uploads each image (`POST /upload/image`) and runs `graphs/flux3-klein-edit.json`, ComfyUI's
`image_flux3_klein_distilled_image_edit` template (served by Raven at `/templates/`) minus the bounding-box region
edit: every reference goes through `ReferenceMultiLatent` (resized to ~1 MP) on both positive and negative
conditioning, and the sampler starts from an empty latent. That is reference conditioning, not img2img on a noisy
canvas. Refs are `<ref_image_0>`, `<ref_image_1>`... in the prompt, which should be the full caption of the result with
the edit folded in; the upsampler (the template's editing system prompt) sees the refs too. `--vision` also feeds the
first three refs to the text encoder (`TextEncodeQwenImageEditPlus`, no vae). Node ids 12-14 (+21...) are the contract
with the client. Each ref is validated before anything is uploaded (a regular file after symlink resolution, png/jpeg/webp by magic number, at most 20 MiB;
`--vision` needs `--ref`; `--ref` and `--graph flux3-klein-edit` go together) and uploaded under a sanitised name
`raven-image-<random>-<name>`. **Uploaded refs persist in ComfyUI's input folder on Raven**; the client never deletes them, so do not
send an image you would not leave on Deru's desktop. Not built: region edits (`CreateBoundingBoxes` + `Flux3LayoutPrompt`).

Test 2026-11-11, Kiko's r1a 12-cell pose sheet as the only reference, same seed: identity carried over in both modes
(mask, bell and tassel, kimono, hair, eyes, tail). `--vision` was closer to the sheet (face, smile, tail, embroidery)
but also pasted three of the sheet's cells into the frame; use one cropped cell as the reference. Images and verdict:
`/opt/commons/handoff/raven-ref-edit-20261111/`.

## `--margin PCT`: clear edges by construction

```
raven-image "a goldfish mascot on a plain pale cream background" --no-enhance --margin 6 --bg fcf7ee -o fish.png
raven-image ... --margin 6 --bg 12fa5f --alpha -o sticker.png      # RGBA, backdrop keyed out
```

The border band (PCT of each axis, rounded up to the 16 px latent grid, 6% of 1024 is 64 px) is held flat during
diffusion, not cleaned afterwards. The client replaces the empty latent with a `--bg`-coloured image run through the
graph's own VAE (`EmptyImage` > `VAEEncode`) and puts a `SetLatentNoiseMask` on it that is 0 on the band and 1 inside
(`SolidMask` + `MaskComposite`, nodes 30-35; node 6 goes). ComfyUI's sampler re-imposes the masked-out latent at every
step at that step's noise level (RePaint-style), so the model composes the interior around a frame it cannot change.
Core nodes only; no custom node, no Raven deploy. Denoise stays 1. Works on `flux3-klein` and `flux3-klein-edit`
(with or without `--vision`). Put the same backdrop in the prompt ("plain pale cream background"); the pin sets the band,
the prompt sets what the interior tries to match.

After the render the client measures the band against its own backdrop (the per-channel median of the band: the VAE
shifts a saturated `--bg` by tens of levels, `#12fa5f` came back `#1fef6d`; cream stayed within 2), repaints the whole
band with that colour (if the median is more than 48 levels from `--bg` on any channel the band is mostly content, and
`--bg` is the reference instead, so a band of subject cannot pass as its own backdrop; the guarantee is a flat band within 48 levels of `--bg`, so a band that is uniformly 30 levels off, which the VAE can do to a saturated colour, passes), and prints it with the per-side count of pixels that were content:
`margin 6.0%: backdrop #fef9ec, cleared 802 of 245760 band px (0.33%; left 646 top 0 right 156 bottom 0)`.
If more than `--max-clip` percent of the band (default 0.5) was content the file is still written, already cleared, and
the client exits **65**: the subject ran into the frame, so it is cut flat at the seam. Re-seed or ask for more room.
`-n` renders all images, then exits 65 if any was dirty (a later Busy still exits 75 and says how many earlier images were dirty; an unreadable render is reported, skipped, and exits 1 at the end). `--size` must be multiples of 16 and the margin must leave an interior of at least 16 px per axis. `--alpha` floods from the band through backdrop-coloured pixels
and makes them transparent (hard key; pure Python, about 1 s). The output is RGBA only with `--alpha`.

Measured 2026-11-11 (1024x1024, 6%, one render each, same seed and ref as the unpinned image):

| case | unpinned: band that was content | pinned |
|---|---|---|
| Aqua's facelift seed 2203 (`--ref --vision`, seaweed and bubbles in the corners) | 33% | 0.33% (corners clean; character unchanged) |
| Kiko r1a at a desk, seed 11 (green) | 31% (the desk ran off the bottom) | 0.43% (the desk now ends inside the frame) |

Pinned and unpinned images, the comparison, and the raw (unrepainted) pinned renders `*-pin6-raw.png` that the pinned numbers are measured on: `/opt/commons/handoff/raven-ref-edit-20261111/clear-edge/`. The images show the result, not the per-step mechanism; that rests on ComfyUI's sampler (its denoise-mask path), and the evidence here is the band: 0 to 0.5% content in nine pinned renders against 31 to 33% unpinned. The pin
did not visibly degrade either render. The residue is subject touching the seam (fin, tail, desk corner), not clutter.
Limits: a hard key leaves a faint backdrop-coloured fringe on antialiased outlines (visible on saturated green, faint on
cream); backdrop enclosed by the subject (the gap between a headband and the head) stays opaque; a saturated key colour
the subject also contains (green in a green-eyed character) is untouched except where connected to the band; the band is
a rectangle, so the pin cannot ask for a vignette or a round sticker. A subject cannot be asked to stay off the seam: only
the exit code tells you it did not.

Offline test (fake ComfyUI on 127.0.0.1, 158 checks): `python3 tests/test-raven-image.py`.

## What it does

The service is ComfyUI's own HTTP API at `http://10.77.0.4:8190`
(`RAVEN_URL` overrides), reachable from Yggdrasil only. Setup and the off switch
are in `runbooks/image-playground-raven.md` ("Mind service exposure").

1. `GET /system_stats` (5 s timeout) as the reachability probe.
2. `POST /prompt` with the graph, patched with size, seed and the prompt.
3. Poll `GET /history/<id>` every 2 s until an image or an error appears.
4. `GET /view` for the PNG and write it (ComfyUI embeds the graph in the PNG).
5. After the last image, if `GET /queue` shows nothing running or pending,
   `POST /free {"unload_models": true, "free_memory": true}` so Raven's VRAM is
   given back. `--keep-loaded` skips this.

Images are `PreviewImage` outputs (ComfyUI's temp dir), so they do not land in
Deru's playground history.

## Raven is busy

Raven is Deru's desktop. He turns the service off by closing the ComfyUI console.
Any connection failure (refused, unroutable, or a read timeout, including mid-render) prints
`raven-image: Raven may be busy or off (...). Not retrying; try again later.` and exits
**75**. It cannot tell the causes apart. The likely one after a Raven reboot is the lock screen: the ComfyUI task needs someone logged in on Raven's desktop (`runbooks/image-playground-raven.md`, "Limits"). There is no retry loop: the caller decides when to ask again. A graph
ComfyUI rejects, a failed render or a timeout exit 1 with the server's message.

Be a polite guest: render in small batches, do not run several clients at once
(the GPU is shared with Deru's games, Bonsai, Ollama).

## Graphs

`graphs/flux3-klein.json` is ComfyUI's API format for the FLUX.3 Klein distilled
t2i template, copied from Raven's `/playground` page `graph()` on 2026-11-10
(euler/simple, 16 steps, cfg 1, empty negative; BFL's prompt upsampler `Pup`
with its system prompt; `TARGET CANVAS: w:h` header). The client patches only
node 6 (size), 7 (seed) and 11 (upsampler prompt), or removes nodes 10-11 and
sets node 4's text for `--no-enhance`. Node ids are the contract between the
client and the graph. If the playground's graph changes, re-copy it.

Graphs are found next to the script (`graphs/`) or in `RAVEN_GRAPHS`.
Install: put this directory under `/opt/commons/opt/raven-image/` and symlink
`raven-image` into `/opt/commons/bin/` (root-owned; ask `@eureka`).

## Licence: concept art only until ruled

Text read 2026-11-10 from `huggingface.co/black-forest-labs/flux-3-image-klein-comfy`
(`LICENSE.md`, FLUX Kommunity License v.1.0; the model card is gated, the
`resolve/main/LICENSE.md` URL answered 200). Quotes, not legal advice:

- **Outputs:** "Output does not include any components of the FLUX Model". Outputs are
  not Derivatives ("Outputs are not considered Derivatives under this License").
  "We claim no ownership rights in and to the Outputs. You are solely responsible for
  the Outputs you generate and their subsequent uses in accordance with this License."
- **Commercial use of outputs** (2.a): only "solely if you are a Qualifying User, use
  Outputs ... for commercial purposes". "Qualifying User" means "you, if at any time
  you together with your affiliates' gross annualized revenue is less than
  US$5,000,000". Otherwise use is limited to "Non-Commercial Purposes", which excludes
  "use (a) for revenue-generating activity (including ... in a production
  environment), (b) in direct interactions with or that has impact on end users".
- **Conditions for a Qualifying User** (2.e): "implement and maintain content
  filtering measures" or "ensure Output undergoes review for unlawful or infringing
  content before public or non-public distribution", and "ensure Output includes
  disclosures ... that the Output was generated or modified using artificial
  intelligence technologies to the extent required under applicable law".
- **Training on outputs** (4.b): you will not "use the FLUX Model, any Derivatives, or
  any Output or results from the FLUX Model or Derivative to improve any other
  artificial intelligence model that performs any function similar to a FLUX Model".
  Derivative (iii) also covers models trained on "synthetic-data Outputs".
- **Derivatives and distribution:** fine-tunes and the weights may be Distributed only
  with the licence copy and the Attribution Notice (3.a-b); the licence is
  "non-transferable, non-sublicensable". Shipping *weights* is not what we do.
- **Shipping outputs in a product:** the text has no clause about it. It is commercial
  use of Outputs, so 2.a and 2.e govern. GameCult's revenue vs the US$5M line, who
  counts as an affiliate, and what a Mind's concept art feeding a shipped product
  counts as are not answered by the text and need a ruling from whoever holds the
  legal root. Termination is automatic on breach (8.a).
- Section 4.a also bars "any data produced by the FLUX Model" for "military purposes",
  surveillance, biometric processing, and for infringing third-party rights,
  "digital replica" rights included: no likenesses of real people.

Check run 2026-11-10: `raven-image "concept art: a lighthouse on a basalt cliff at dusk, storm clouds, a small ferry below" -n 2` made two 1536x1024 images (81 s cold, 45 s warm), then freed VRAM.

Until that ruling: concept art, not shipped assets, and nothing generated here goes
into a training set.
