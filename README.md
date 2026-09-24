# DBLP to Zotero

An Alfred workflow for lazy computer scientists.

## Usage

Type

    dbz [query]

and this workflow will search DBLP for papers that match the given query (by title or authors) and show them in Alfred. Several actions may then be performed:

* Return: Add selected paper to Zotero (needs some configuration: keep reading!)

* Quick look (left shift): Show DBLP record page of selected paper in a Quick Look window

* Cmd + Return: Open DBLP URL of selected paper in the default browser

* Alt + Return: Copy DBLP URL of selected paper to clipboard

### Offline search

Searches and Zotero imports use a local copy of DBLP when one is installed, and
only go online for what it cannot answer. To download the latest monthly DBLP
dump and build the local copy, run:

    uv run main.py update-db

and re-run it whenever you want a newer copy; `--release YYYY-MM-DD` picks a
specific release. A build takes about 6 minutes, uses up to half of your RAM,
and needs about 25 GB of free disk space while it runs; afterwards the download
and temporary files are deleted, and the local copy takes about 5.5 GB. The dump is published monthly by
[Schloss Dagstuhl](https://drops.dagstuhl.de/entities/collection/10.4230/dblp.xml)
under CC0. With a local copy installed:

* Results come from the local copy, followed by a *Search DBLP online* item.
* Queries with no local results are sent to DBLP automatically.
* End a query with `!` (e.g. `dbz model checking!`) to search DBLP online
  directly, for papers newer than your copy.

The copy lives in Alfred's workflow data folder
(`~/Library/Application Support/Alfred/Workflow Data/com.github.lou1306.alfred-dbz/`),
not in the workflow folder, so it is not synced with your Alfred preferences.
Set `DBZ_DATA_DIR` to put it elsewhere.

## Installing

This workflow requires Python 3.7 or higher, with the following libraries: `click`, `PyZotero`, `requests`, `xmltodict` (see `requirements.txt`). You also need a Zotero account with an [API key](https://www.zotero.org/support/dev/web_api/v3/basics).

1. Install Python 3.7 or higher
2. Install the workflow
3. Right-click on workflow, then "Open in Terminal"
4. In Terminal: `pip3 install -r requirements.txt`
5. Go back to Workflow screen, click on "Configure workflow and variables" (the *[x]* in the top-right corner)
6. Insert the following values in the "Workflow Environment Variables" pane:
  * `python3path`: path to your Python3 interpreter. You should be able to find it by typing `which python3` in a terminal window
  * `ZOTEROID`, `ZOTEROKEY`: ID and API key of your Zotero account.

## Acknowledgements

This work would not exist if not for the incredible work of the Zotero and DBLP teams. If you enjoy using this workflow, please consider [a donation to CHNM](http://chnm.gmu.edu/donate/) (Zotero's home), or [a paid Zotero account](https://www.zotero.org/storage).

Also, kudos to the Alfred team for developing a great tool!

## That's all

Open an issue if you encounter problems while using this workflow.

Good luck with your research! :)
