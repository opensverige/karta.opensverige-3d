# Terms for building a cell

Version 2026-11-25. `rutor login` prints these ten lines and asks you to accept them. If an agent runs the commands for you, it shows you these lines first and passes `--accept-terms` only after you said yes. Each claim and upload carries the version you said yes to, and the service keeps it with them. When the version changes, a claim or an upload stops until you say yes again with `rutor terms`.

1. Building a cell is voluntary and unpaid. Nobody owes you anything for it, and you can stop at any time.
2. You run it on your own machine, at your own cost and risk. The software comes as is, without warranty, under the MIT license.
3. Only the redrawn PNG and its manifest leave your machine. The source render stays with you.
4. You let the project show your cell on karta.opensverige.se, change it, blend it with its neighbours and remove it. As far as the rights are yours to give, you give them under the MIT license.
5. A person reviews your cells before they go on the map. Only a builder the maintainer has marked trusted goes straight through. Any cell can be rejected, or taken down later.
6. Your GitHub login is shown publicly on your cells, with the time each cell last changed. Open an issue to have it removed.
7. The login asks GitHub for no permissions (an empty scope). The service uses your token only to ask GitHub who you are, and does not store it.
8. The redraw model is under the Qwen Research License: non-commercial research and evaluation only. You download and run it under that license yourself. The LoRA is Apache 2.0, and its model card says it was trained on renders of Google's 3D tiles.
9. A sheet with open data as its source is drawn from a public orthophoto and terrain model and from OpenStreetMap (© OpenStreetMap contributors, ODbL). Today that is goteborg-norr alone: Göteborgs Stad's orthophoto and terrain model (CC0).
10. Every other sheet, stockholm-norr among them, is drawn from Google's 3D tiles through your own Cesium ion account, whose terms bind you, not this project. Google's Map Tiles policy does not allow deriving new content from its tiles: read it and decide for yourself. Google can close your access and ask for those cells to be taken down.

## Where the other terms are

- This project's code: [LICENSE](LICENSE) (MIT).
- The model: <https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo/blob/main/LICENSE>. The LoRA: <https://huggingface.co/handful/mndf-pixel-qwen-image-2.1>.
- OpenStreetMap: <https://www.openstreetmap.org/copyright>.
- Google's Map Tiles policy: <https://developers.google.com/maps/documentation/tile/policies>. Cesium ion: <https://cesium.com/legal/terms-of-service/>.

This page is not legal advice. It says what the project asks of you and what it knows about the sources.
