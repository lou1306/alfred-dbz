#!/usr/bin/env python3

from json import JSONDecoder
from sys import stderr, exit
from os import environ

from pyzotero import zotero, zotero_errors
import requests
import xmltodict
import click


J = JSONDecoder()


def extract_text(node, join=True):
    if isinstance(node, str):
        return node
    elif isinstance(node, dict):
        return node["#text"]
    elif isinstance(node, list):
        return "; ".join(
            extract_text(n) for n in node) if join else extract_text(node[0])
    else:
        raise KeyError(f"Cannot find text for {node}.")


def get(key):
    print(f"Getting {key} from DBLP...", file=stderr)
    response = requests.get(f"https://dblp.org/rec/xml/{key}.xml")
    if response.status_code != 200:
        raise KeyError("Key not found in DBLP.")
    record = xmltodict.parse(response.text)
    dblp_type = list(record["dblp"].keys())[0]
    info = record["dblp"][dblp_type]
    return info, dblp_type


def query_dblp(qry_string):
    print(f"Querying DBLP for {qry_string}...", file=stderr)
    response = requests.get(
        "http://dblp.org/search/publ/api",
        {"format": "json", "q": qry_string, "c": 10})
    if response.status_code != 200:
        raise Exception("DBLP request failed: {}".format(response.status_code))
    hits = J.decode(response.text)["result"]["hits"]
    total = int(hits["@total"])
    first = int(hits["@first"]) + 1
    last = first + int(hits["@sent"]) - 1
    url = response.url
    return [x["info"] for x in hits.get("hit", [])], total, first, last, url


def make_creator(name, creator_type='author'):
    PREFIXES = ["di", "de", "della", "del", "von", "van", "der", "ter"]
    names = [n for n in name.split(" ") if not n.isdigit()]
    *first_names, last_name = names
    while first_names and first_names[-1].lower() in PREFIXES:
        last_name = f"{first_names.pop()} {last_name}"
    first_name = " ".join(first_names)

    return {
        'creatorType': creator_type,
        'firstName': first_name,
        'lastName': last_name
    }


"""
zotero types:
'artwork', 'audioRecording', 'bill', 'blogPost', 'book', 'bookSection', 'case',
'computerProgram', 'conferencePaper', 'dictionaryEntry', 'document', 'email',
'encyclopediaArticle', 'film', 'forumPost', 'hearing', 'instantMessage',
'interview', 'journalArticle', 'letter', 'magazineArticle', 'manuscript',
'map', 'newspaperArticle', 'note', 'patent', 'podcast', 'presentation',
'radioBroadcast', 'report', 'statute', 'tvBroadcast', 'thesis',
'videoRecording', 'webpage'
"""


def convert_type(dblp_type):
    return {
        "book": "book",
        "inproceedings": "conferencePaper",
        "article": "journalArticle",
        "incollection": "bookSection",
        "phdthesis": "thesis"
    }.get(dblp_type, "document")


@click.group()
def group():
    pass


@group.command()
@click.argument("qry_string", required=True)
def alfred_lookup(qry_string):
    def fmt(hit):
        if not isinstance(hit["authors"]["author"], list):
            authors = [hit["authors"]["author"]["text"]]
        else:
            authors = [i["text"] for i in hit["authors"]["author"]]
        if len(authors) > 3:
            authors = authors[:3]
            authors.append("et al.")

        venue = f'{hit.get("venue", "<No venue>")}, {hit["year"]}'
        return f"""{{
        "uid": "{hit["key"]}",
        "title": "{hit["title"]}",
        "subtitle": "{", ".join(authors)} ({venue})",
        "arg": "{hit["key"]}",
        "quicklookurl": "https://dblp.uni-trier.de/rec/bibtex/{hit["key"]}",
        "icon": {{
            "path": "dblp-logo.png"
        }}
        }}"""
    try:
        infos, *_ = query_dblp(qry_string)
        hits = (fmt(hit) for hit in infos)
        print(f"""{{ "items": [{','.join(hits)}] }}""")
    except Exception as exc:
        print(f"""{{ "items": [{{
              "title": "Lookup failed for {qry_string}",
              "subtitle": "{exc}"}}
        ]}}""")


@group.command()
@click.argument("key", required=True)
@click.option("--silent", default=False)
def add_to_zotero(key, silent):
    add_to_zotero_fn(key, silent)


def add_to_zotero_fn(key, silent):
    ID, KEY = environ.get("ZOTEROID", ""), environ.get("ZOTEROKEY", "")
    try:
        zot = zotero.Zotero(ID, "user", KEY)
    except zotero_errors.MissingCredentials:
        print(
            "There was an error with Zotero API authentication. "
            "Please check your Zotero ID and API key."
            )
        exit(1)
    info, dblp_type = get(key)

    author = info.get("author", [])
    if isinstance(author, str) or isinstance(author, dict):
        author = [extract_text(author)]

    creators = [make_creator(extract_text(i)) for i in author]
    zot_type = convert_type(dblp_type)
    template = zot.item_template(zot_type)

    mapping = {
        "date": "year",
        "DOI": "ee",
        "extra": "@key",
        "ISBN": "isbn",
        "issue": "number",
        "pages": "pages",
        "publicationTitle": "journal",
        "publisher": "publisher",
        "title": "title",
        "university": "school",
        "url": "ee",
        "volume": "volume"
    }

    post_process = {
        "DOI": lambda x: x[16:] if "https://doi.org/" in x else "",
        "extra": lambda x: f"Citation Key: DBLP:{x}",
        "title": lambda x: x[:-1] if x[-1] == "." else x,
    }

    for k1, k2 in mapping.items():
        if k1 in template:
            join = k1 not in ("DOI", "url")
            template[k1] = extract_text(info.get(k2, template[k1]), join)
    for k, f in post_process.items():
        if k in template:
            template[k] = f(template[k])

    editors = info.get("editor", [])
    if isinstance(editors, str) or isinstance(editors, dict):
        editors = [extract_text(editors)]
    creators.extend(make_creator(extract_text(i), "editor") for i in editors)

    if "crossref" in info.keys():
        crf, _ = get(info["crossref"])
        editors = crf.get("editor", [])
        if not isinstance(editors, list):
            editors = [editors]
        creators.extend(make_creator(
            extract_text(i), "editor") for i in editors)

        crf_mapping = {
            "bookTitle": "title",
            "proceedingsTitle": "title",
            "publisher": "publisher",
            "series": "series",
            "volume": "volume"
        }
        for k1, k2 in crf_mapping.items():
            if k1 in template:
                template[k1] = extract_text(crf.get(k2, template[k1]))

    template["creators"] = creators

    zot.create_items([template])
    if not silent:
        print(f"Added {key} to Zotero.")
    return template


@group.command()
@click.argument("qry_string", required=False)
@click.option("--key", default=None)
@click.option("--skip-zotero", default=False, is_flag=True)
def cli(qry_string, skip_zotero, key):
    def fmt(hit):
        if not isinstance(hit["authors"]["author"], list):
            authors = [hit["authors"]["author"]["text"]]
        else:
            authors = (i["text"] for i in hit["authors"]["author"])
        venue = f'{hit.get("venue", "<No venue>")}, {hit["year"]}'
        return f'\t{", ".join(authors)}\n\t{hit["title"]}\n\t({venue})'

    if not key:
        if not qry_string:
            qry_string = click.prompt("DBLP query")
        infos, total, first, last, _ = query_dblp(qry_string)
        if infos:
            print(
                "## DBLP search result ##",
                f'Showing hits {first}-{last} of {total}',
                "\n".join(
                    f"[{i}]{fmt(x)}"
                    for i, x in enumerate(infos, start=1)),
                sep="\n"
            )
            choices = click.Choice([str(i) for i in range(1, len(infos) + 1)])
            PROMPT = "Choose an item"
            index = click.prompt(PROMPT, type=choices, show_choices=False)
            key = infos[int(index) - 1]["key"]
        else:
            print("No matches found.")
            exit(0)

    template = add_to_zotero_fn(key, True)
    print("----------", file=stderr)
    print(
        "\n".join(f"{k}:\t\t {v}" for k, v in template.items() if v),
        file=stderr)
    print("----------", file=stderr)


if __name__ == '__main__':
    group()
