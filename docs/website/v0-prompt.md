# Meldra OS website — v0 prompt

Paste everything below the line into v0 as one prompt. Upload these screenshots with it, so v0 shows the real
system instead of faking it:

- `docs/screenshots/gnome-overview.png`, `gnome-desktop.png`, `gnome-app-grid.png`, `gnome-terminal.png`,
  `gnome-settings.png`, `gnome-network.png`, `meldra-installer.png`, `meldra-login.png`, `labwc-first-frame.png`

Re-check the facts in "Content" against the README before each regeneration; they change as milestones land.

---

Build a complete, production-quality marketing and documentation website for **Meldra OS**, an experimental
open-source operating system. Use Next.js (App Router), TypeScript, Tailwind CSS and shadcn/ui. Every page must be
fully responsive (down to 360 px wide), support light and dark themes (dark is the default), meet WCAG 2.2 AA
contrast, work with keyboard only, and respect `prefers-reduced-motion`.

## Brand

- **Name:** Meldra OS (always "Meldra OS" on first mention, "Meldra" after).
- **Tagline:** "Linux compatibility. BSD discipline. One carefully engineered system."
- **Idea:** two streams merging into one. The mark is a teal ribbon (from the lower left) and a peach ribbon (from
  the lower right) curving up and joining into a single vertical stem, inside a thin lavender circle broken by a
  gap at the top and one at the bottom. Draw it as a clean inline SVG logo (mark plus "Meldra" wordmark).
- **Palette:**
  - Night indigo `#151633` (dark background), deep navy `#0E1026` (darkest surfaces)
  - Teal `#5FC9C3` (Linux stream, primary accent), Peach `#F0A27A` (BSD stream, secondary accent)
  - Lavender `#A99FD6` (rings, borders, subtle highlights), Mist `#F4F2FB` (light-mode background)
  - Use teal→peach gradients sparingly: logo, hero headline underline, primary button hover.
- **Type:** Inter (UI and body), JetBrains Mono (code, terminal snippets, version numbers).
- **Mood:** calm, precise, trustworthy, slightly warm. Soft glows and gentle gradients, not neon. Generous spacing.
  No stock photos of people, no generic "cyber" imagery, no fake testimonials, no fake download counts.

## Images to generate

Generate these as original artwork in the brand style (dark indigo ground, teal and peach light ribbons, lavender
accents, subtle film grain, no text inside images):

1. **Hero background** (wide, 2400×1200): two luminous ribbons, teal from the left and peach from the right,
   converging into one bright stream at the centre-right, fading into deep indigo. Leave the left half calm for the
   headline.
2. **"How it works" illustration:** a layered diagram-like render — a solid teal foundation slab labelled by the
   page (not in the image) as Linux, a lavender middle layer, and small peach modules slotting in from above.
3. **Hearth icon art:** a minimal glowing hearth/flame formed from a single teal-to-peach line.
4. **Kiln icon art:** a minimal kiln/arched furnace outline with a package box inside, same line style.
5. **Open Graph image** (1200×630): the mark centred on indigo with "Meldra OS" in Inter below it.
6. **Four milestone badges** (square): Accessibility, Hyprland, COSMIC, KDE Plasma — abstract line icons in the
   ribbon style, one accent each.

Use the uploaded screenshots exactly as they are (inside a subtle device/window frame with rounded corners and a
soft lavender glow). Never redraw or mock up the OS UI.

## Site map

`/` Home · `/features` · `/download` · `/roadmap` · `/docs` (landing that links to the repository docs) ·
`/about` · plus a 404 page. Shared header (logo, nav, theme toggle, "GitHub" button, "Get the preview" primary
button) and footer (nav columns, licence note "Hearth is GPL-2.0; components keep their upstream licences",
"Meldra OS is a developer preview").

## Home page sections

1. **Hero:** tagline as H1, sub-line "An experimental OS that keeps Linux's hardware and app compatibility while
   adopting selected BSD designs — one reviewed subsystem at a time." Buttons: "Get the developer preview" →
   `/download`, "Read the roadmap" → `/roadmap`. A small pill above the H1: "Developer preview · not yet a daily
   driver". Hero art behind, GNOME overview screenshot floating on the right with a glow.
2. **Status strip:** four compact stats in mono font — `Linux 6.18 LTS`, `glibc 2.44`, `GNOME Shell 50.6`,
   `Hearth PID 1`.
3. **Two streams, one system:** three cards — "Linux is the compatibility spine", "BSD work is admitted, not
   assumed", "Upstream updates are the security unit" (copy in Content below). Include the "How it works"
   illustration.
4. **See it running:** a screenshot carousel/gallery (overview, desktop, app grid, terminal, settings, quick
   settings, installer, login) with captions; keyboard and swipe navigable, with a lightbox.
5. **What works today:** a clean status table (Area / State / Implementation) with coloured state chips
   (green = Running/Working, teal = Built/Tested, amber = Preview).
6. **Meldra's own pieces:** Hearth and Kiln feature cards with their icon art, plus a terminal-styled block
   showing boot status lines:
   ```
   [  OK  ] Mounted early filesystems
   [  OK  ] Started D-Bus System Message Bus
   [  OK  ] Started Login and seat manager (elogind)
   [  OK  ] Started Network Manager
   [  OK  ] Started Login screen (greetd)
   ```
   (OK in green, styled like a real console.)
7. **Install in minutes:** three steps — boot the live ISO, open "Install Meldra", choose disk / account /
   optional encryption — with the installer screenshot.
8. **What's next:** milestone timeline teaser (M1–M4) linking to `/roadmap`.
9. **Call to action band:** gradient ribbon background, "Try Meldra in a VM today", buttons to download and GitHub.

## Other pages

- **/features:** sections for Desktop (GNOME Shell, Settings, Console, Files, Text Editor), Graphics (Mesa 26:
  virgl, radeonsi/RADV, nouveau, zink; Intel planned), Networking (NetworkManager, WPA2/WPA3), Audio (PipeWire,
  WirePlumber), Bluetooth (BlueZ 5.88), Accessibility (AT-SPI; Orca screen reader in progress), Install & login
  (UEFI, ext4, optional LUKS2, install alongside another OS, ReGreet login), Power (suspend/resume, power off).
  Each with an honest status chip.
- **/download:** a single universal live ISO card (x86_64, UEFI and BIOS) with placeholder size, SHA-256 field
  and a "copy checksum" button; system requirements (x86_64, 4 GB RAM, 20 GB disk; Secure Boot off); a "Try it in
  QEMU" code block; a clear warning box that this is a developer preview and the graphical installer erases the
  chosen disk. Release notes accordion.
- **/roadmap:** vertical timeline of milestones with checkbox lists:
  - **M1 — Accessibility, website, login, boot output:** Orca screen reader and acceptance tests; this website;
    a redesigned login screen; systemd-style `[ OK ]` boot status lines (done).
  - **M2 — Hyprland:** Hyprland session and ISO.
  - **M3 — COSMIC:** System76's COSMIC desktop.
  - **M4 — KDE Plasma:** the latest Plasma 6 release.
  Below it, the engineering gates with progress bars: Command-line OS ~15%, Desktop OS ~5% (label them "approximate
  engineering readiness, not calendar promises").
- **/docs:** cards linking to Architecture, Boot, Desktop, Security, Roadmap and Licensing documents in the
  repository (use `#` placeholders for URLs), plus a "Build from source" quick-start code block:
  `make check`, `make kernel`, `make packages`, `make gnome-iso`.
- **/about:** project rules ("Keep the system bootable after every integration step", "Port one subsystem at a
  time behind an explicit boundary"), the architecture diagram rebuilt as an accessible SVG (Linux 6.18 LTS
  drivers + ABI / Meldra userspace Hearth + Kiln / Desktop profiles → reviewed subsystem adapters → selected BSD
  designs), and a security contact section placeholder.

## Content (use verbatim where possible)

- "Meldra OS is an experimental operating system that keeps Linux's hardware and application compatibility while
  introducing selected BSD designs through reviewed, testable subsystem boundaries."
- "This is not a textual splice of two kernel trees. Meldra starts from a pinned Linux LTS baseline, then evaluates
  BSD-derived components one subsystem at a time. Every import needs clear provenance, compatible licensing,
  measurable benefit, tests, and a rollback path."
- Linux is the compatibility spine: "Drivers, the syscall ABI, and mainstream Linux applications keep working
  throughout development."
- BSD work is admitted, not assumed: "`components.toml` records provenance, ownership, licensing, integration
  boundaries, and maturity."
- Upstream updates are the security unit: "Meldra avoids an unauditable pile of permanent one-off patches."
- Hearth: "Meldra's compact PID 1 and service supervisor. It mounts the early filesystems, launches declarative
  services, reaps child processes, respawns long-running services, reports each one's status at boot, and performs
  orderly shutdown."
- Kiln: "Meldra's package format and package manager. Packages carry explicit dependencies and are installed
  through traversal-safe, conflict-aware archive handling — small enough to audit."
- Honesty line (show on Home and Download): "Meldra is an installable developer preview, not yet a daily-driver
  OS. Broader hardware testing, recovery, signed updates, and security/release engineering remain."

## Quality bar

Semantic HTML, one H1 per page, descriptive alt text for every screenshot, visible focus rings in lavender,
`next/image` for all images, metadata and Open Graph tags per page, no layout shift, Lighthouse 95+ across the
board. Subtle motion only: ribbons in the hero drift slowly; cards lift slightly on hover; everything static when
reduced motion is requested.
