# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Xuân Trường Giang  **MSSV:** 2A202602446  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.
>
> Provider: `openrouter:openai/gpt-4o-mini` (chat) + `openrouter:openai/text-embedding-3-small` (embedding), top_k=3, chunk_size=800.
> `ket_qua_benchmark_kg.txt` = ontology tự thiết kế (mặc định). `ket_qua_benchmark_kg.hint.txt` = ontology gợi ý (`KG_ONTOLOGY=hint`), chỉ dùng để so sánh trước/sau.

## 1. Chi phí (10 điểm)

Dán 2 bảng `Indexing` và `Querying` từ `ket_qua_benchmark_kg.txt`:

```
Chat model: openrouter:openai/gpt-4o-mini | Embedding: openrouter:openai/text-embedding-3-small | top_k=3 | chunk_size=800 | chunks=176 | KG: 219 nodes / 510 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112    116.9
graph       196     96218     5430   0.01040    211.7

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       45   0.00012     2.41
graph       0.94   1.83     2125       98   0.00037     3.69
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00112 | 0.01040 | ×9.3 |
| Indexing giây | 116.9 | 211.7 | ×1.8 |
| Mỗi câu: USD | 0.00012 | 0.00037 | ×3.1 |
| Mỗi câu: giây | 2.41 | 3.69 | ×1.5 |
| Mỗi câu: in_tok | 694 | 2125 | ×3.1 |

**Chi phí tăng thêm đến từ đâu?**
> **Indexing:** phần chênh = dựng KG = 196 − 176 = **20 lần gọi LLM trích xuất** (1 lần/bài báo), 40 146 in_tok + 5 430 out_tok,
> **$0.00928** (89% chi phí indexing của GraphRAG) và ~95 giây. Phần luật (18 Điều, 99 khoản, 239 ngưỡng) dựng bằng regex nên **$0**.
> Embedding chunk (176 lần gọi) dùng chung cho cả hai pipeline.
> **Mỗi câu hỏi:** không có lần gọi LLM nào thêm (vẫn 1 lần/câu) — chi phí tăng hoàn toàn do **prompt dài hơn**: +1 431 in_tok/câu là các
> dữ kiện graph (khung hình phạt, đối chiếu khối lượng, tóm tắt vụ, cạnh 1 bước). Thời gian +1.3 s/câu = vài truy vấn Cypher (vài chục ms)
> + LLM đọc prompt dài hơn và viết dài hơn (98 vs 45 out_tok).
>
> **Điểm hòa vốn / khấu hao:** GraphRAG đắt hơn $0.00025/câu và tốn thêm $0.00928 một lần. Sau **≈ 37 câu hỏi**
> ($0.00928 / $0.00025) chi phí dựng KG mới bằng phần chênh truy vấn, tức chi phí dựng chiếm phần lớn cho đến khoảng vài chục câu.
> Tính theo **chi phí trên một câu trả lời đúng** ở nhóm cross-kb (Q3–Q5): Flat đạt judge 0+0+1 (0 câu đúng đủ) nên chi phí/câu đúng là vô hạn;
> GraphRAG đạt 2+2+2 với $0.00096 cho 3 câu → **$0.00032/câu đúng**. Với dữ liệu này KG "hòa vốn" ngay khi có câu hỏi xuyên KB.
>
> So với ontology gợi ý (`.hint.txt`): GraphRAG mỗi câu **4604 → 2125 in_tok** và **$0.00073 → $0.00037** (gợi ý nhét nguyên văn toàn bộ
> khoản luật, kể cả mọi điểm a, b, c…; bản tự thiết kế chỉ đưa dòng đầu của khoản + điểm khớp khối lượng).

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa (Flat rẻ hơn) | Định nghĩa nằm gọn trong 1 chunk Điều 2 Luật PCMT; graph chỉ lặp lại (dữ kiện `Term`). |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa (Flat rẻ hơn) | Tên 2 bị cáo tử hình nằm trong cùng 1 chunk bài báo. |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | **Graph** | Flat lấy được chunk bài báo nhưng không có chunk Điều 251 → "Không đủ thông tin"; graph đi `Person→ACCUSED_OF→Crime←DEFINES←Article` ra khoản 1 "02 năm đến 07 năm". |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 2 | **Graph** | "Hoàng Nato" là biệt danh → seed qua `Person.aliases`, tới Điều 255 và `max_penalty` "tù 20 năm hoặc tù chung thân". |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 1.00 / 2 | **Graph** | Flat đoán đúng "điểm b, 20 năm…tử hình" từ chunk của một Điều khác nhưng không nêu được Điều 250/khoản 4; graph so `amount_g = 9600 ≥ min_g = 100` trên cạnh `THRESHOLD`. |
| Q6 | aggregation | 0.00 / 1 | 0.67 / 1 | Graph (một phần) | Flat chỉ thấy 3 chunk nên nói "cả ba vụ…" (Đức, Thành, Đông) không có tên vụ; graph liệt kê được 5 vụ có `INVOLVES MDMA` nhưng không ghi tên "Lê Minh Thành" (xem E4). |

**Quy luật:** câu **single-hop** (đáp án nằm trong 1 đoạn) → hai bên ngang nhau, Flat rẻ hơn ×3. Câu **cross-kb** (đáp án ghép từ tin + luật) →
Flat sụp về 0 vì top-k chỉ lấy chunk giống câu hỏi (tên người) nên không bao giờ kéo theo chunk luật; GraphRAG thắng tuyệt đối nhờ cạnh cầu nối.
Câu **multi-hop có điều kiện số** (Q5) → chỉ ontology có thuộc tính số (`amount_g`, `min_g/max_g`) mới chọn đúng khoản. Câu **aggregation**
→ graph tốt hơn (duyệt được mọi vụ, không bị giới hạn top-k) nhưng kết quả phụ thuộc chất lượng trích xuất và chính phép đo.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật — câu hỏi "phạt tối đa" chỉ nhận được khung cơ bản (ontology gợi ý)

- **Hiện tượng:** Q4 hỏi mức phạt tù **tối đa** cho "Hoàng Nato". Với ontology gợi ý, GraphRAG trả lời mức của khoản 1, thiếu "chung thân" (recall 0.67, judge 1),
  dù graph có đủ 4 khoản của Điều 255.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q4 graph:

```
--- Q4 [cross-kb] graph recall=0.67 judge=1 2.21s
Giang hồ 'Hoàng Nato' bị bắt về hành vi tổ chức sử dụng trái phép chất ma túy. Hành vi này có thể bị phạt tù từ 02 năm đến 07 năm theo Điều 255 BLHS.
```

Quy tắc lọc của KG-3 gợi ý là "khoản 1 + khoản `MENTIONS` một chất mà vụ `INVOLVES`". Điều 255 không nhắc tên chất nào:

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl)-[:MENTIONS]->(s) RETURN cl.number AS n, collect(s.name) AS subs
```

```
(0 dòng)    -- report/evidence_hint_graph.txt, mục "Q4 255 clauses mentioned"
```

- **Nguyên nhân:** lỗi ở **thiết kế ontology + Cypher KG-3**: ontology không có khái niệm "khung cao nhất", và quy tắc chọn khoản giả định mọi tội
  đều phân khung theo chất. Tội "tổ chức sử dụng" phân khung theo số người/đối tượng, nên quy tắc chỉ còn khoản 1.
- **Đề xuất sửa (đã làm trong ontology tự thiết kế):** khi regex luật, ghi `Article.max_clause`/`max_penalty` (khoản có hình phạt tù cuối cùng);
  KG-3 luôn đưa khoản 1 + khoản cao nhất khi câu hỏi có ý định pháp lý. Kết quả sau sửa (`ket_qua_benchmark_kg.txt`):

```
--- Q4 [cross-kb] graph recall=1.00 judge=2 3.50s
Giang hồ 'Hoàng Nato' (Dương Minh Tuấn) bị bắt về hành vi tổ chức sử dụng trái phép chất ma túy. Hành vi này có thể bị phạt tù tối đa 20 năm hoặc tù chung thân theo khoản 4 Điều 255 Bộ luật Hình sự.
```

  Đánh đổi: +1 dòng/Điều trong prompt (~40 token). Lấy hết khoản thì đủ nhưng prompt gấp đôi (đúng như bản gợi ý: 4604 in_tok/câu).

### Lỗi E3: Trùng thực thể — chất đồng nghĩa và vụ việc bị nhân bản

- **Hiện tượng:** cùng một chất thành nhiều node `Substance`; cùng một vụ thành nhiều node `Case`.
- **Bằng chứng (ontology gợi ý):**

```cypher
MATCH (s:Substance) RETURN s.name AS name, COUNT { (s)<-[:INVOLVES]-() } AS cases ORDER BY toLower(s.name)
```

```
chất ma túy (1) | Ketamine (3) | ketamine (3) | ma túy (3) | MDMA (5) | Methamphetamine (1) | methamphetamine (2) | thuốc lắc (1) | ...
-- 16 node, trong đó Ketamine/ketamine, Methamphetamine/methamphetamine, thuốc lắc/MDMA là trùng; "ma túy", "chất ma túy" vô nghĩa
```

  Ontology tự thiết kế (cùng truy vấn, `evidence_custom_graph.txt`): 13 node, không còn cặp trùng; `Ketamine` 6 vụ, `MDMA` 6 vụ.
  Nhưng `Case` vẫn trùng:

```cypher
MATCH (p:Person {name:'Cái Quang Huy'})-[:INVOLVED_IN]->(k:Case) RETURN k.id, k.name, k.stage
```

```
news-100260918080821054#1 | Vụ vận chuyển ma túy của Cái Quang Huy  | khác
news-100260917203001265#0 | Vụ vận chuyển ma túy từ Đức về Việt Nam | truy tố
```

  `news-100260918080821054` là bài về **Lê Minh Thành**; dòng 92 của file là đoạn "tin liên quan":
  *"Từ mối quen biết khi cùng làm bếp tại một nhà hàng ở Berlin (Đức), Cái Quang Huy bị cáo buộc…"*. Tương tự bài
  "Công nhân nói không với ma túy" (`news-100260918220613301`, dòng 42) dính đoạn về "Đức Cộng" → sinh vụ "Vụ bắt giữ Nguyễn Minh Đức" không có tội danh.
- **Nguyên nhân:** (1) **khóa định danh trong ontology gợi ý** là tên do LLM tự viết, không chuẩn hóa hoa/thường hay đồng nghĩa nên `MERGE` coi
  "ketamine" ≠ "Ketamine"; (2) **crawl** giữ cả khối "tin liên quan" cuối bài, LLM coi đó là một vụ của bài.
- **Đề xuất sửa:** (1) đã làm — `canonical_substance` (bảng đồng nghĩa "thuốc lắc/kẹo→MDMA", "ma túy đá→Methamphetamine", gộp hoa/thường bằng
  `link_entity`, bỏ "ma túy" chung chung), và gộp `Person` qua biệt danh nhiều từ (`Dương Minh Tuấn` ← "Hoàng Nato", 4 vụ).
  (2) chưa làm — cắt khối "tin liên quan" trong `scripts/crawl_drug_corpus.py` (chọn đúng thẻ nội dung bài), hoặc gộp `Case` khi trùng
  (người, chất, khối lượng). Đánh đổi: bảng đồng nghĩa phải bảo trì tay; gộp vụ theo khóa mềm có thể gộp nhầm 2 vụ cùng người.

### Lỗi E1: Cầu nối nối nhầm — `link_entity` map hành vi không phải tội vào một tội

- **Hiện tượng:** báo chí hay viết "sử dụng trái phép chất ma túy" (vi phạm hành chính, BLHS không có tội này). `link_entity` với cutoff 0.8
  map nó vào "**tổ chức** sử dụng trái phép chất ma túy" (Điều 255) → người chỉ sử dụng bị gắn khung 02–07 năm tù.
- **Bằng chứng:**

```python
>>> link_entity('sử dụng trái phép chất ma túy', crimes)
'tổ chức sử dụng trái phép chất ma túy'          # difflib ratio ≈ 0.88 > 0.8
>>> link_crime('sử dụng trái phép chất ma túy', crimes)
None
```

  Ngược lại, vụ không nên nối vẫn bị bỏ đúng: `MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.name, k.doc_id` trên graph gợi ý chỉ ra
  "Vụ tông cảnh sát giao thông ở An Giang" (tài xế dùng ma túy, không phải tội về ma túy) — **không nối là đúng**.
- **Nguyên nhân:** `link_entity` (KG-1) đo độ giống ký tự; hai tên tội chỉ khác nhau ở vế định danh "tổ chức" nên rất giống nhau.
- **Đề xuất sửa (đã làm):** `link_crime` = `link_entity` + chốt chặn "chuỗi trích được là phần con thực sự của tên chuẩn thì từ chối"; prompt dặn LLM
  bỏ qua hành vi không có trong danh sách. Đánh đổi: có thể từ chối cách viết tắt hợp lệ (vd "tổ chức sử dụng ma túy" vẫn qua, nhưng "mua bán ma túy" bị trả `None` — mất cầu cho vụ đó).

### Lỗi E4: Phép đo sai — câu trả lời đúng nhưng recall thấp, đáp án chuẩn thiếu

- **Hiện tượng:** Q6 graph recall 0.67, judge 1, dù câu trả lời liệt kê **đủ cả 3 vụ** trong đáp án chuẩn.
- **Bằng chứng:** `ket_qua_benchmark_kg.txt`, Q6 graph (rút gọn):

```
1. Vụ vận chuyển ma túy từ Đức về Việt Nam: Cái Quang Huy … hơn 9,6kg MDMA.
2. Vụ góp tiền mua ma túy tại Hà Nội: Có 5 viên nén màu trắng MDMA …
3. Vụ tổ chức sử dụng ma túy tại Sầm Sơn: Có 0,686g MDMA …
4. Vụ án tại Viện Pháp y tâm thần Trung ương: Có liên quan đến MDMA …
5. Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây ma túy: Có MDMA trong vụ này.
```

  `must_include = ["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`: vụ (2) đúng là vụ Lê Minh Thành nhưng được gọi bằng tên vụ → trượt từ khóa.
  Mục (5) bị coi là "thừa", nhưng `data/drug_news/news-100260920221957595.md` dòng 30 viết *"…mua bán ma túy loại etomidate, ketamine, thuốc lắc…"* —
  thuốc lắc chính là MDMA, nên **đáp án chuẩn thiếu vụ này**. Ngược lại, Q5 Flat recall 0.60 / judge 1 là đo đúng: câu trả lời nói "khoản b)" mà không nêu Điều 250.
- **Nguyên nhân:** **phép đo**: recall là khớp chuỗi (nhạy với cách gọi tên), và gold của Q6 được soạn trước khi biết "thuốc lắc" = MDMA.
- **Đề xuất sửa:** chấp nhận nhiều biến thể trong `must_include` (tên người hoặc tên vụ), bổ sung vụ Hoàng Nato vào gold Q6, và dặn judge
  "không trừ điểm mục thừa nếu có căn cứ trong nguồn". Đánh đổi: soạn benchmark tốn công hơn; judge mềm hơn dễ chấm nới.

## 4. Kết luận (5 điểm)

> **Nên dùng KG khi:** câu hỏi cần **ghép dữ kiện từ hai nguồn không cùng đoạn văn** (vụ án ↔ điều luật) hoặc cần **điều kiện có cấu trúc**
> (khối lượng ≥ ngưỡng → khoản nào; liệt kê mọi vụ có chất X). Trên 3 câu cross-kb, Flat RAG đạt recall 0.00 / 0.00 / 0.60 và judge 0 / 0 / 1,
> GraphRAG đạt 1.00 / 1.00 / 1.00 và judge 2 / 2 / 2. Trung bình 6 câu: recall **0.43 → 0.94**, judge **1.00 → 1.83**, đổi lại chi phí mỗi câu ×3.1
> ($0.00012 → $0.00037), độ trễ ×1.5 (2.41 s → 3.69 s) và chi phí dựng ×9.3 ($0.00112 → $0.01040). KG đáng tiền khi (a) một nguồn đủ đều để trích
> bằng regex (luật: 0 USD), (b) tỉ lệ câu hỏi xuyên nguồn cao, (c) số câu hỏi đủ lớn để khấu hao ~$0.009 dựng graph (≈ vài chục câu).
>
> **Flat RAG là đủ khi:** đáp án nằm trong một đoạn (Q1, Q2: cả hai đạt recall 1.00 / judge 2) — khi đó GraphRAG chỉ tốn thêm ×3 token mà không
> thêm đúng. Thiết kế ontology quyết định phần lớn chất lượng: cùng dữ liệu, cùng LLM, ontology tự thiết kế so với gợi ý tăng judge 1.67 → 1.83
> mà **giảm** 49% chi phí mỗi câu, vì prompt chỉ chứa đúng khoản cần thiết thay vì toàn văn các khoản.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.09s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openrouter:openai/gpt-4o-mini | embedding = openrouter:openai/text-embedding-3-small
[OK] KG-2 build_graph: 163 node / 387 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 17 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00081. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (đường đi Person → Case → Crime ← Article, kèm `THRESHOLD` khoản 4 ↔ MDMA, Ketamine, Hà Nội).

## Vấn đề gặp phải (không tính điểm)

> - Kết quả trích xuất bằng LLM thay đổi giữa các lần chạy dù temperature 0: một lần chạy thử trước đó vụ Lê Minh Thành không có `INVOLVES MDMA`
>   (Q6 graph recall 0.33); sau khi prompt dặn "liệt kê ĐỦ mọi chất, kể cả khi chỉ có số viên" thì có lại. Số liệu trong báo cáo lấy từ lần chạy cuối.
> - LLM đôi khi ghi nguyên văn chuỗi "chuỗi rỗng" vào trường mức án (bắt chước mô tả trong prompt) — đã lọc trong code (`_value`).
