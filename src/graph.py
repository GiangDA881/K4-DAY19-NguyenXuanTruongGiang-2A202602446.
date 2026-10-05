"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(unicodedata.normalize("NFC", name or ""))
    if not target:
        return None
    by_norm: dict[str, str] = {}
    for original in known:
        by_norm.setdefault(normalize(unicodedata.normalize("NFC", original)), original)
    if target in by_norm:
        return by_norm[target]
    close = difflib.get_close_matches(target, list(by_norm), n=1, cutoff=0.8)
    return by_norm[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Custom ontology (report/ONTOLOGY.md): what changes vs the HINT and why
#   - Substance synonyms ("thuốc lắc", "ma túy đá", "ketamin"...) are folded into one canonical node.
#   - (Clause)-[:THRESHOLD {point, min_g, max_g}]->(Substance) models the quantity ranges of each khoản;
#     (Case)-[:INVOLVES {amount, amount_g}]->(Substance) carries the seized mass in grams, so Cypher can
#     pick the khoản a case falls into. Article.max_clause/max_penalty = the heaviest frame.
#   - (Person)-[:ACCUSED_OF {case_id, stage, sentence}]->(Crime): per-person charge + procedural stage,
#     a direct bridge Person -> Crime instead of a free-text property on INVOLVED_IN.
#   - Case is keyed by doc_id#i (stable) instead of the LLM-made name; Person aliases merge across articles.
#   - (Term)-[:DEFINED_IN]->(Article) for the definitions in Luật PCMT Điều 2.
# ----------------------------------------------------------------------------------------------

OTHER_SOLID = "chất ma túy khác (thể rắn)"
SUBSTANCE_ALIASES = {
    "heroin": "Heroine", "hêrôin": "Heroine", "cocain": "Cocaine", "côcain": "Cocaine",
    "ma túy đá": "Methamphetamine", "ma tuý đá": "Methamphetamine", "đá": "Methamphetamine",
    "methamphetamin": "Methamphetamine", "meth": "Methamphetamine", "amphetamin": "Amphetamine",
    "thuốc lắc": "MDMA", "kẹo": "MDMA", "ecstasy": "MDMA", "ketamin": "Ketamine", "ke": "Ketamine",
    "nhựa cần sa": "cần sa", "cần sa khô": "cần sa", "marijuana": "cần sa",
    "nhựa thuốc phiện": "thuốc phiện", "nha phiến": "thuốc phiện",
}
GENERIC_SUBSTANCES = {"ma túy", "ma tuý", "chất ma túy", "chất ma tuý", "ma túy các loại"}
LOCATION_ALIASES = {"tp.hcm": "TP.HCM", "tphcm": "TP.HCM", "hcm": "TP.HCM", "hồ chí minh": "TP.HCM",
                    "sài gòn": "TP.HCM", "hà nội": "Hà Nội"}
LEGAL_INTENT = re.compile(r"[Đđ]iều|khoản|khung|hình phạt|phạt tù|tội|luật", re.IGNORECASE)
EMPTY_VALUES = {"chuỗi rỗng", "không có", "không rõ", "n/a", "none", "null", "chưa có"}
STAGES = ["bắt giữ","khởi tố", "truy tố", "xét xử sơ thẩm", "xét xử phúc thẩm", "khác"]
POINT_START = re.compile(r"^([a-zđ])\)\s", re.MULTILINE)
LAW_RANGE = re.compile(r"từ ([\d,]+) (gam|kilôgam) đến dưới ([\d,]+) (gam|kilôgam)")
LAW_MIN = re.compile(r"([\d,]+) (gam|kilôgam) trở lên")
NEWS_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilôgam|kilogam|kilogram|ký|gram|gam|gr|g)\b", re.IGNORECASE)

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "").strip().strip("\"'“”‘’").lower())

def _value(text: Any) -> str:
    """LLM placeholders ('chuỗi rỗng', 'không có'...) -> ''."""
    text = str(text or "").strip()
    return "" if _norm(text) in EMPTY_VALUES else text

def _law_grams(value: str, unit: str) -> float:
    return float(value.replace(",", ".")) * (1000 if unit == "kilôgam" else 1)

def amount_to_grams(text: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0, '406g' -> 406.0, '5 viên' -> None (Vietnamese: ',' decimal, '.' thousands)."""
    match = NEWS_AMOUNT.search(text or "")
    if not match:
        return None
    number = match.group(1)
    if "." in number and len(number.split(".")[-1]) == 3:
        number = number.replace(".", "")
    value = float(number.replace(",", "."))
    return value * 1000 if match.group(2).lower() in {"kg", "kilôgam", "kilogam", "kilogram", "ký"} else value

def canonical_substance(name: str) -> str:
    """Fold synonyms onto SUBSTANCES; unknown names stay (lower-cased); '' for generic 'ma túy'."""
    lowered = _norm(name)
    bare = re.sub(r"\s*\(.*?\)", "", lowered).strip()
    for key in (lowered, bare):
        if key in SUBSTANCE_ALIASES:
            return SUBSTANCE_ALIASES[key]
    found = find_substances(lowered)
    found = [s for s in found if not any(s != o and s.lower() in o.lower() for o in found)]  # Amphetamine ⊂ Methamphetamine
    if len(found) == 1:
        return found[0]
    linked = link_entity(bare, SUBSTANCES, normalize=_norm)
    if linked:
        return linked
    return "" if bare in GENERIC_SUBSTANCES else bare

def canonical_location(name: str) -> str:
    """'TP Hồ Chí Minh' / 'TP.HCM' -> 'TP.HCM'; 'tỉnh Ninh Bình' -> 'Ninh Bình'."""
    stripped = re.sub(r"^(thành phố|tp\.?|tỉnh)\s*", "", (name or "").strip(), flags=re.IGNORECASE).strip()
    return LOCATION_ALIASES.get(_norm(name)) or LOCATION_ALIASES.get(_norm(stripped)) or stripped

def link_crime(name: str, known: list[str]) -> str | None:
    """link_entity + a guard: a mention that is a strict part of the match lacks its qualifier.
    'sử dụng trái phép chất ma túy' (administrative, not a crime) must NOT become 'tổ chức sử dụng ...'."""
    linked = link_entity(name, known)
    if linked and normalize_crime(name) != normalize_crime(linked) and normalize_crime(name) in normalize_crime(linked):
        return None
    return linked

def parse_law_article_custom(doc: Document) -> dict[str, Any]:
    """HINT regex extraction + quantity thresholds per điểm, heaviest frame, and PCMT term definitions."""
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["first_line"] = re.sub(r"^\d+\.\s*", "", clause["text"].splitlines()[0])
        thresholds, mentions = [], set()
        points = list(POINT_START.finditer(clause["text"]))
        for index, point in enumerate(points):
            end = points[index + 1].start() if index + 1 < len(points) else len(clause["text"])
            text = re.sub(r"\s+", " ", clause["text"][point.start():end]).strip()
            substances = find_substances(text) + ([OTHER_SOLID] if "chất ma túy khác ở thể rắn" in text else [])
            between, at_least = LAW_RANGE.search(text), LAW_MIN.search(text)
            if substances and (between or at_least):
                if between:
                    low, high = _law_grams(*between.group(1, 2)), _law_grams(*between.group(3, 4))
                else:
                    low, high = _law_grams(*at_least.group(1, 2)), None
                thresholds += [{"substance": s, "point": point.group(1), "min_g": low, "max_g": high, "text": text}
                               for s in substances]
            else:
                mentions.update(substances)
        thresholded = {t["substance"] for t in thresholds}
        mentions.update(s for s in find_substances(clause["text"]) if s not in thresholded)
        clause["thresholds"], clause["mentions"] = thresholds, sorted(mentions - thresholded)
    prison = [c for c in article["clauses"] if "tù" in c["penalty"]]
    article["max_clause"] = prison[-1]["number"] if prison else None
    article["max_penalty"] = prison[-1]["penalty"] if prison else ""
    article["terms"] = []
    if "giải thích từ ngữ" in article["title"].lower():
        for clause in article["clauses"]:
            flat = re.sub(r"\s+", " ", clause["text"])
            match = re.match(r"\d+\.\s*(.+?) là ", flat)
            if match:
                article["terms"].append({"name": match.group(1).strip(), "definition": re.sub(r"^\d+\.\s*", "", flat)})
    return article

NEWS_EXTRACTION_PROMPT_CUSTOM = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài, không suy đoán. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt, nêu chất và khối lượng nếu có",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "stage": "giai đoạn tố tụng MỚI NHẤT bài nói tới, một trong: {stages}",
  "charges": ["tội danh của vụ, chọn NGUYÊN VĂN từ DANH SÁCH TỘI DANH; hành vi không có trong danh sách (vd: sử dụng ma túy) thì bỏ qua"],
  "substances": [{{"name": "liệt kê ĐỦ mọi chất bài nêu cho vụ này, kể cả khi chỉ có số viên hoặc không có khối lượng; dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp (thuốc lắc/kẹo = MDMA, ma túy đá = Methamphetamine)",
                   "amount": "khối lượng nguyên văn trong bài, ví dụ: hơn 9,6kg; chuỗi rỗng nếu không có"}}],
  "people": [{{"name": "họ tên đầy đủ (không kèm biệt danh)", "aliases": ["biệt danh, tên gọi khác, ví dụ: Hoàng Nato"],
               "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charges": ["tội danh của RIÊNG người này, nguyên văn từ DANH SÁCH TỘI DANH; [] nếu bài không nói"],
               "sentence": "mức án của người này nếu có, ví dụ: tử hình, 36 tháng tù; chuỗi rỗng nếu chưa xét xử"}}]
}}]}}
Một bài có thể có nhiều vụ việc độc lập; cùng một vụ thì gộp làm một. Bài không nói về vụ việc cụ thể
(tuyên truyền, hội nghị, khen thưởng chung chung...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases_custom(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction; code then canonicalises crimes, substances (+ grams), locations and person names."""
    prompt = NEWS_EXTRACTION_PROMPT_CUSTOM.format(
        stages=" | ".join(STAGES), crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    clean = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        people = []
        for person in case.get("people") or []:
            name = (person.get("name") or "").strip()
            aliases = [a.strip().strip("\"'“”‘’") for a in person.get("aliases") or [] if a and a.strip()]
            nickname = re.match(r"(.+?)\s*\((.+)\)$", name)               # "Dương Minh Tuấn (Hoàng Nato)"
            if nickname:
                name, aliases = nickname.group(1).strip(), aliases + [nickname.group(2).strip().strip("\"'“”‘’")]
            if not name:
                continue
            people.append({
                "name": name, "aliases": sorted(set(aliases) - {name}), "role": person.get("role") or "",
                "sentence": _value(person.get("sentence")),
                "charges": sorted({c for c in (link_crime(x, known_crimes) for x in person.get("charges") or []) if c}),
            })
        substances: dict[str, dict] = {}
        for s in case.get("substances") or []:
            name = canonical_substance(s.get("name") or "")
            if name and name not in substances:
                amount = _value(s.get("amount"))
                substances[name] = {"name": name, "amount": amount, "amount_g": amount_to_grams(amount)}
        charges = {c for c in (link_crime(x, known_crimes) for x in case.get("charges") or []) if c}
        charges |= {c for p in people for c in p["charges"]}   # a person's charge is also a charge of the case
        stage = case.get("stage") or ""
        clean.append({
            "name": case.get("name") or "", "summary": case.get("summary") or "", "date": case.get("date") or "",
            "location": canonical_location(case.get("location") or ""), "stage": stage if stage in STAGES else "khác",
            "charges": sorted(charges), "substances": list(substances.values()), "people": people,
        })
    return clean

def merge_person_aliases(cases: list[dict], registry: dict[str, str]) -> None:
    """Same person, different articles: 'Hoàng Nato' vs 'Dương Minh Tuấn' -> one canonical name (in place).
    Only multi-word names/aliases are used as merge keys, so a bare 'Thành' never merges two people."""
    for case in cases:
        for person in case["people"]:
            keys = [person["name"]] + [a for a in person["aliases"] if len(a.split()) >= 2]
            canonical = next((registry[_norm(k)] for k in keys if _norm(k) in registry), person["name"])
            for key in keys:
                registry.setdefault(_norm(key), canonical)
            person["aliases"] = sorted(({person["name"]} | set(person["aliases"])) - {canonical})
            person["name"] = canonical

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- custom ontology: writes

    def custom_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "id"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name"), ("Term", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article_custom(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id})
              SET a.title = $title, a.law = $law, a.doc_id = $doc_id,
                  a.max_clause = $max_clause, a.max_penalty = $max_penalty
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text,
                  cl.first_line = clause.first_line, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.mentions | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            FOREACH (t IN clause.thresholds | MERGE (sub:Substance {name: t.substance})
                MERGE (cl)-[r:THRESHOLD {point: t.point}]->(sub)
                SET r.min_g = t.min_g, r.max_g = t.max_g, r.text = t.text)
            """,
            **article,
        )
        if article["terms"]:
            self.run(
                """
                MATCH (a:Article {id: $id})
                UNWIND $terms AS t
                MERGE (term:Term {name: t.name}) SET term.definition = t.definition, term.doc_id = $doc_id
                MERGE (term)-[:DEFINED_IN]->(a)
                """,
                id=article["id"], terms=article["terms"], doc_id=article["doc_id"],
            )

    def add_news_case_custom(self, case: dict, doc: Document, case_id: str) -> None:
        self.run(
            """
            MERGE (k:Case {id: $case_id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.stage = $stage,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.amount_g = s.amount_g)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = [a IN coalesce(person.aliases, []) WHERE NOT a IN p.aliases] + p.aliases
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.sentence = p.sentence
                FOREACH (crime IN p.charges | MERGE (c:Crime {name: crime})
                    MERGE (person)-[acc:ACCUSED_OF {case_id: $case_id}]->(c)
                    SET acc.stage = $stage, acc.sentence = p.sentence))
            """,
            case_id=case_id, name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), stage=case.get("stage", ""),
            location=case.get("location", ""), charges=case["charges"], substances=case["substances"],
            people=case["people"], doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if ontology() == "hint":
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_custom(question, doc_ids, max_facts)

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        seed_ids, facts = self.seed_facts(question, doc_ids, limit=max_facts)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        facts += [f"Vụ việc '{c['name']}': {c['summary']}" for c in cases]
        clauses = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ')
              AND (cl.number = 1 OR EXISTS { (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $substances })
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[c["id"] for c in cases], numbers=re.findall(r"[Đđ]iều (\d+)", question),
            substances=find_substances(question),
        )
        facts += [f"[{c['article']} - {c['title']}] khoản {c['number']}: {c['text']}" for c in clauses]
        return facts

    def _context_custom(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        seed_ids, seed = self.seed_facts(question, doc_ids, skip_labels=("Clause", "Term"), limit=40)
        facts: list[str] = []

        # 1. Legal terms named in the question (Luật PCMT Điều 2 definitions).
        for row in self.run(
            """
            MATCH (t:Term)-[:DEFINED_IN]->(a:Article)
            WHERE toLower($q) CONTAINS toLower(t.name)
            RETURN a.id AS article, t.definition AS definition
            """,
            q=question,
        ):
            facts.append(f"[{row['article']}] Định nghĩa: {row['definition']}")

        # 2. People named in the question: Person -ACCUSED_OF-> Crime <-DEFINES- Article (the bridge, per person).
        people = self.run(
            """
            MATCH (p:Person)-[r:INVOLVED_IN]->(k:Case)
            WHERE elementId(p) IN $ids
            OPTIONAL MATCH (p)-[acc:ACCUSED_OF {case_id: k.id}]->(c:Crime)
            OPTIONAL MATCH (c)<-[:DEFINES]-(a:Article)
            RETURN p.name AS name, p.aliases AS aliases, elementId(k) AS case_id, k.name AS case_name,
                   r.role AS role, r.sentence AS sentence, acc.stage AS stage, c.name AS crime, a.id AS article
            """,
            ids=seed_ids,
        )
        for p in people:
            aliases = f" (còn gọi: {', '.join(p['aliases'])})" if p["aliases"] else ""
            crime = f"tội {p['crime']} ({p['article']})" if p["crime"] else "chưa rõ tội danh"
            facts.append(f"{p['name']}{aliases}: {p['role'] or 'liên quan'} trong vụ '{p['case_name']}'; {crime}; "
                         f"giai đoạn: {p['stage'] or 'không rõ'}; mức án: {p['sentence'] or 'chưa có'}")

        # 3. Cases that are a seed or one hop from a seed (person, substance, location, chunk doc_id).
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            OPTIONAL MATCH (k)-[i:INVOLVES]->(s:Substance)
            WITH k, collect(s.name + CASE WHEN coalesce(i.amount, '') = '' THEN '' ELSE ' ' + i.amount END) AS subs
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary, k.stage AS stage, subs,
                   [(p:Person)-[r:INVOLVED_IN]->(k) WHERE r.role <> 'cán bộ' | p.name][..6] AS people
            LIMIT 15
            """,
            ids=seed_ids,
        )
        # Aggregation: a substance named in a question about no particular person -> every case that INVOLVES it.
        by_substance: dict[str, list[str]] = {}
        for row in [] if people else self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance)
            WHERE elementId(s) IN $ids
            RETURN s.name AS substance, k.name AS case_name, i.amount AS amount,
                   [(p:Person)-[r:INVOLVED_IN]->(k) WHERE r.role <> 'cán bộ' | p.name][..4] AS people
            ORDER BY case_name
            """,
            ids=seed_ids,
        ):
            amount = f" ({row['amount']})" if row["amount"] else ""
            who = f", người: {', '.join(row['people'])}" if row["people"] else ""
            by_substance.setdefault(row["substance"], []).append(f"'{row['case_name']}'{amount}{who}")
        for substance, items in by_substance.items():
            facts.append(f"Các vụ việc có {substance} ({len(items)} vụ): " + "; ".join(items))

        focus = list(dict.fromkeys(p["case_id"] for p in people)) or [c["id"] for c in cases]
        if not LEGAL_INTENT.search(question):   # "ai lãnh án?", "vụ nào có MDMA?": no penalty frames needed
            focus = []

        # 4. Legal basis. Articles = crimes of the named people, else crimes of the focus cases, + "Điều N" in question.
        articles = [p["article"] for p in people if p["article"]] or [r["id"] for r in self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)
            WHERE elementId(k) IN $focus RETURN DISTINCT a.id AS id
            """, focus=focus)]
        articles = articles if focus else []
        articles += [r["id"] for r in self.run(
            "MATCH (a:Article) WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ') RETURN a.id AS id",
            numbers=re.findall(r"[Đđ]iều (\d+)", question))]
        articles = list(dict.fromkeys(articles))
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.id IN $articles AND (cl.number = 1 OR cl.number = a.max_clause)
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.first_line AS line,
                   cl.number = a.max_clause AS is_max
            ORDER BY article, number
            """,
            articles=articles,
        ):
            frame = "khung cao nhất" if row["is_max"] else "khung cơ bản"
            facts.append(f"[{row['article']} - {row['title']}] khoản {row['number']} ({frame}): {row['line']}")

        # 5. Quantity thresholds: the clause whose [min_g, max_g) range contains the seized amount.
        #    Substances the article does not name (e.g. Ketamine) fall back to "chất ma túy khác (thể rắn)".
        for row in self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance)
            WHERE elementId(k) IN $focus AND i.amount_g IS NOT NULL
            MATCH (k)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)-[t:THRESHOLD]->(x:Substance)
            WHERE a.id IN $articles
              AND (x = s OR (x.name = $other AND NOT EXISTS { (a)-[:HAS_CLAUSE]->(:Clause)-[:THRESHOLD]->(s) }))
              AND i.amount_g >= t.min_g AND (t.max_g IS NULL OR i.amount_g < t.max_g)
            RETURN DISTINCT k.name AS case_name, s.name AS substance, i.amount AS amount, a.id AS article,
                   cl.number AS number, cl.penalty AS penalty, t.point AS point, t.text AS text
            ORDER BY article, number
            """,
            focus=focus, articles=articles, other=OTHER_SOLID,
        ):
            facts.append(f"Đối chiếu khối lượng: {row['substance']} {row['amount']} trong vụ '{row['case_name']}' "
                         f"thuộc [{row['article']}] khoản {row['number']} điểm {row['point']} ({row['text']}) "
                         f"=> {row['penalty']}")

        for c in cases:
            subs = f" Chất: {', '.join(c['subs'])}." if c["subs"] else ""
            who = f" Người: {', '.join(c['people'])}." if c["people"] else ""
            facts.append(f"Vụ việc '{c['name']}' (giai đoạn: {c['stage'] or 'không rõ'}): {c['summary']}{subs}{who}")
        return list(dict.fromkeys(facts + seed))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    json_llm = lambda prompt: llm_fn(prompt, json_mode=True)  # noqa: E731
    if ontology() == "hint":
        graph.suggested_constraints()
        articles = [parse_law_article(d) for d in law_docs]
        for article in articles:
            graph.add_law_article(article)
        crimes = [a["crime"] for a in articles if a["crime"]]
        for doc in news_docs:
            for case in extract_news_cases(doc, json_llm, crimes):
                graph.add_news_case(case, doc)
        return

    graph.custom_constraints()
    articles = [parse_law_article_custom(d) for d in law_docs]
    for article in articles:
        graph.add_law_article_custom(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    registry: dict[str, str] = {}
    for doc in news_docs:
        cases = extract_news_cases_custom(doc, json_llm, crimes)
        merge_person_aliases(cases, registry)
        for index, case in enumerate(cases):
            graph.add_news_case_custom(case, doc, case_id=f"{doc.id}#{index}")

def ontology() -> str:
    """KG_ONTOLOGY=hint rebuilds the suggested ontology (for the before/after comparison); default = custom."""
    return os.getenv("KG_ONTOLOGY", "custom").strip().lower()

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(c["metadata"].get("doc_id", c["id"]) for c in chunks))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts) or "(không có)",
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
