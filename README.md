# ViberHive

ViberHive is a Windows tray app for finding reaction GIFs and short quote clips for Discord. Press **Ctrl+F12** to open the picker over your current chat. Choose a GIF or clip, review it in your Discord draft, and send it when ready.

## Install on Windows 11

1. Install standard 64-bit Python 3.13 for Windows (x64). Python 3.12 and 3.14 x64 are also supported.
2. On the repository page, select **Code → Download ZIP** and extract the entire archive to a folder you intend to keep. You can also clone the repository.
3. Open **Setup.cmd**. It downloads the required packages, checks media playback, and configures the shortcut for this folder.
4. Open **ViberHive.lnk**. To pin it, right-click the shortcut and choose **Pin to Start** or **Show more options → Pin to taskbar**. You can also launch the picker directly with **Run_ViberHive.cmd**.

**Start_in_Tray.cmd** starts ViberHive in the background. Press **Esc** or close the picker to hide it. To exit, right-click its tray icon and select **Quit ViberHive**.

## Find and share media

| Action | How |
| --- | --- |
| Search | Open **GIFs** or **Clips**, type a phrase, and press **Enter**. Search also starts after a five-second typing pause. |
| Preview and paste a GIF | Hover to preview, then click the tile to paste into the current Discord desktop chat. For manual paste, right-click the tile and choose **Copy GIF file**, then press **Ctrl+V** in the chat. |
| Share a quote clip | Search for a line of dialogue under **Clips**. Click a result to preview it, then choose **Paste into Discord** to share its link. |
| Find related GIFs | Right-click a result and choose **More like this** to search its title. On a Google result, **Visually similar images** opens matches for that image. |
| Keep a favorite | Click the star on a tile. Use **Favorites** to find it again or organize it into a folder. |

## Search providers

**Google Images**, **Wikimedia Commons**, and **KLIPY** offer GIF search without an API key. Eligible Google and Commons results paste as animated files. KLIPY results paste as links. **Yarn.co** and **Clip.Cafe** provide quote clips.

Optional SerpApi and KLIPY keys can be entered in **Settings** or supplied through the environment variables `SERPAPI_API_KEY` and `KLIPY_APP_KEY`.

### If Google asks for sign-in

Choose **Verify with Google…** and complete the prompt in the window that opens. A sign-in message can still appear after a successful CAPTCHA, including while Google is loading results. If the picker stops there, **Open in browser** continues the same search in your default browser. To save a GIF you find there, copy its original image address and choose **Favorites → GIFs → Add GIF by URL…**. **Search KLIPY** continues the search inside the picker.

## Local data and privacy

ViberHive keeps favorites, search history, settings, and cached media in `%LOCALAPPDATA%\ViberHive`. To back up your favorites, quit ViberHive from the tray and copy that folder. Project updates can reuse the same profile.

Search providers receive your searches, and media is fetched from the sites hosting it. The embedded Google browser keeps its cookies in memory for the session. Windows protects API keys saved in Settings for the current Windows user. Automatic paste uses the clipboard and the Discord desktop window. You control when the message is sent.
