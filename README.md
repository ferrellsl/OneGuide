# OneGuide

OneGuide builds one XMLTV guide file for an HDHomeRun tuner. It combines two sources:

- **Gracenote** (tvlistings.gracenote.com): 5 or more days of listings with descriptions, cast, crew,
  genres and images, but it can miss low-power stations and ATSC 3.0 channels.
- **Your HDHomeRun tuner's own guide** from SiliconDust: every channel the tuner receives, but only
  about 2 days, and no subscription is needed.

Each channel gets Gracenote's listings when Gracenote carries it, and the tuner's guide fills in
the channels Gracenote doesn't. The result is one file that needs no merging. It works with Kodi
(for example with [Direct Tuner PVR](https://github.com/ferrellsl/Direct-Tuner-PVR)), NextPVR,
Jellyfin, Plex, Channels DVR and anything else that reads XMLTV.

OneGuide replaces the usual mix of zap2xml, an HDHomeRun guide grabber and a merge step.

## Before you start: set up a free Gracenote account

OneGuide needs a Gracenote TV listings account. The account is free, and it tells Gracenote which
stations you receive. **Do this first, or OneGuide won't get any listings.**

1. Go to **https://tvlistings.gracenote.com/** in your browser.
2. Choose **Sign in** at the top right, then create an account with your email address and a
   password.
3. While signed in, set your **ZIP code** (or Canadian postal code).
4. Choose your provider. For an antenna and an HDHomeRun, choose the **local broadcast** (over the
   air) lineup.
5. Check that the grid shows your local channels.

The email address and password are what you give OneGuide with `-u` and `-p`. Gracenote uses them
only to find your lineup.

If you mark channels as favorites on the Gracenote site, OneGuide uses only those channels' Gracenote
listings. Add `-a` to use all of them.

## Download

Download `OneGuide.exe` from the [Releases page](https://github.com/ferrellsl/OneGuide/releases). It's
one file and needs no installation. It runs on 64-bit Windows 10 and 11.

On Linux or macOS, download `oneguide.py` from the same page and run it with Python 3.9 or later. It
uses only the standard library.

## First run

Open a Command Prompt in the folder where you put `OneGuide.exe` and run:

```
OneGuide.exe -u you@example.com -p yourpassword -D -I images
```

OneGuide finds the tuner on your network, downloads the listings and writes `xmltv.xml` in the same
folder.

- `-D` adds cast, crew, genres and series images. The first run makes one request per series, so it
  can take 10 minutes or more. After that, details are cached for a week and a run takes about a
  minute.
- `-I images` saves every program image and channel logo in the `images` folder, so the guide doesn't
  depend on web addresses. Leave it out to keep web addresses instead.

Then point your guide software at `xmltv.xml`. In Direct Tuner PVR, this is the **XMLTV guide file**
setting.

## Run it every day

Listings change, so run OneGuide once a day. On Windows, put the command in a batch file, for example
`UpdateGuide.bat` in the same folder:

```bat
@echo off
cd /d "%~dp0"
OneGuide.exe -D -I images -u you@example.com -p yourpassword > oneguide-run.log 2>&1
```

Then add it to Task Scheduler:

1. Open **Task Scheduler** and choose **Create Basic Task**.
2. Name it, choose **Daily**, and pick a time such as 4:00 AM.
3. Choose **Start a program** and select `UpdateGuide.bat`.
4. Open the task's **Properties**. On the **Settings** tab, turn on **Run task as soon as possible
   after a scheduled start is missed**.

Each run writes what happened to `oneguide-run.log`.

The batch file holds your Gracenote password, so keep it in a folder only you can read. Alternatively,
set the environment variables `ONEGUIDE_USER` and `ONEGUIDE_PASSWORD` and leave `-u` and `-p` out.

On Linux, use cron, for example:

```
0 4 * * * cd /home/me/guide && python3 oneguide.py -D -I images >> oneguide-run.log 2>&1
```

## Options

| Option | What it does |
|---|---|
| `-u`, `-p` | Gracenote email address and password. Or set `ONEGUIDE_USER` and `ONEGUIDE_PASSWORD`. |
| `-d DAYS` | Days of Gracenote listings. Default 5. |
| `-t ADDRESS` | The tuner's IP address. Only needed if it isn't found automatically, for example on another subnet. |
| `-o FILE` | Output file. Default `xmltv.xml`. |
| `-D` | Add cast, crew, genres and series images. |
| `-I FOLDER` | Download all images to this folder and use them in the guide. |
| `--image-path PATH` | With `-I`, the folder name to write in the guide, such as a network share like `\\server\guide\images`, so other computers can show the images. |
| `-W WIDTH` | Width to ask Gracenote for program images. Default 720. `0` gets the originals, which can be several MB each. |
| `-A "new live"` | Add ` *` to the titles of new or live programs. |
| `-a` | Use all Gracenote channels, not only your favorites. |
| `-c FOLDER` | Cache folder. Default `cache`. |
| `--no-gracenote` | Use only the tuner's guide. Needs no Gracenote account. |
| `--use-tuner LIST` | Channels that always use the tuner's guide, such as `35.3,44.3`. |
| `--use-gracenote LIST` | Channels that use Gracenote even when its listings disagree with the tuner's. |
| `--tuner-days DAYS` | Days to ask the tuner's guide for. Default 3; SiliconDust usually has about 2. |
| `-S SECONDS` | Wait between requests. |
| `-r TRIES` | Tries per request. Default 3. |
| `-q` | No progress output. |

## How channels are matched

Channels come from your tuner's lineup, and each one is written with its channel number, such as
`3.1`, as its ID and display name. For each channel, OneGuide:

1. Looks for a Gracenote station with the same channel number.
2. Otherwise matches the station ID. This is how ATSC 3.0 simulcasts such as 103.1 get the full
   listings of 3.1.
3. Compares the titles from both sources, if both have listings. If they mostly disagree, Gracenote's
   entry is probably out of date, so OneGuide uses the tuner's guide and says so in its output. Use
   `--use-gracenote` or `--use-tuner` to decide a channel yourself.
4. Fills any gaps in Gracenote's listings from the tuner's guide.

Channels that Gracenote lists but your tuner doesn't receive are left out.

## Build from source

```
pip install pyinstaller
pyinstaller --onefile --name OneGuide --console oneguide.py
```

The program is in `dist`.

## Notes

- Gracenote's listings are for personal use. Don't redistribute the files OneGuide makes.
- OneGuide asks your tuner for its guide the same way the HDHomeRun app does. It needs no SiliconDust
  DVR subscription.
- Gracenote slows down clients that make many requests quickly. If you see "server busy, waiting",
  OneGuide is backing off and will carry on.

## License

MIT. See [LICENSE](LICENSE).

OneGuide is an independent project. It isn't affiliated with or endorsed by Gracenote, Nielsen or
SiliconDust. HDHomeRun is a trademark of SiliconDust USA Inc.
