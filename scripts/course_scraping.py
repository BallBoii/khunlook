"""Simple scraper: CU Get Reg -> Markdown for an embedding model.

Usage (Python 3.8+, no installs needed):
    python scrape_to_markdown.py                 # all 427 GenEd courses
    python scrape_to_markdown.py 0123101 2502390 # just these

Output:
    courses_md/<courseNo>.md   one file per course  (embed each file as one chunk)
    courses.md                 everything in one file, courses separated by '---'

Polite: one request at a time, 1 second apart (~20-30 min for all). Resumable: raw API
responses are cached in courses_raw/, so rerunning skips courses already fetched.
"""
import json, re, sys, time, urllib.request, urllib.error
from pathlib import Path

API = "https://cugetreg.com/_api/graphql"
TERMS = [("2569", "2"), ("2569", "1"), ("2568", "2"), ("2568", "1")]  # newest first
DELAY = 1.0
# The site's firewall rejects Python's default "Python-urllib/3.x" with 403, so identify honestly.
USER_AGENT = "cu-gened-rag/1.0 (Chula student GenEd course-recommendation project)"
RAW, MD_DIR = Path("courses_raw"), Path("courses_md")

COURSES = """
0123101 0123104 0123105 0123106 0201232 0294472 0294473 0294475 0295103 0295111 0295112 0295113 0295114
0295115 0295116 0295139 0299001 0299002 0299003 0299004 0299005 0299006 0299007 0299008 0299009 0299010
2100111 2100225 2100311 2101251 2101256 2102041 2102209 2103201 2104181 2104409 2105261 2107219 2107220
2107221 2109101 2109336 2110191 2110221 2110222 2111201 2111330 2112210 2140140 2140145 2142109 2147102
2147111 2182100 2182205 2183282 2185101 2200183 2200185 2200201 2200222 2200223 2200226 2200227 2200330
2200387 2200389 2200390 2200391 2200392 2200393 2200394 2200395 2200396 2204180 2206101 2207103 2207201
2207203 2207341 2207361 2207363 2207365 2207371 2207385 2207387 2207444 2207467 2207472 2207474 2207478
2209212 2209373 2209375 2210214 2210215 2210216 2210217 2210218 2210219 2210221 2210222 2210225 2210226
2210227 2210235 2210239 2210301 2210313 2210314 2210315 2210316 2210323 2210335 2210420 2210423 2221249
2221316 2221390 2221433 2221485 2222011 2222012 2223023 2223024 2223181 2223243 2224224 2226001 2232241
2232253 2234482 2235320 2236001 2236003 2236103 2236222 2244151 2245151 2296111 2296200 2296228 2296313
2296352 2300150 2300152 2301172 2302190 2303150 2303165 2304274 2305103 2305104 2305106 2305107 2305109
2305120 2305151 2305161 2306416 2307205 2307206 2307230 2307437 2308200 2308303 2308354 2309201 2309202
2309203 2310201 2310202 2311132 2312100 2313210 2313213 2313221 2313226 2314255 2314257 2398131 2400104
2403183 2403184 2403185 2403284 2403471 2404301 2501191 2501192 2501292 2501295 2501296 2501297 2501298
2501299 2502291 2502292 2502330 2502378 2502379 2502390 2502393 2502430 2503101 2503110 2503216 2503469
2504101 2504102 2504345 2506504 2541151 2541152 2541154 2541155 2541156 2541157 2541158 2541159 2541160
2541162 2541163 2541168 2541169 2541171 2541176 2541177 2542001 2542002 2542003 2601111 2602121 2602171
2602241 2603244 2604362 2604365 2605311 2722178 2722221 2722272 2722288 2736106 2737110 2737201 2746104
2746105 2746292 2747406 2750178 2796200 2800118 2800119 2800210 2800211 2800212 2800218 2800219 2800221
2800314 2800381 2801321 2894100 2900151 2900152 3000106 3000257 3000281 3000396 3010101 3011102 3098101
3101101 3102114 3107302 3200106 3200109 3200111 3200116 3200216 3200314 3200316 3202101 3300100 3301102
3303100 3303191 3304102 3305101 3306101 3307101 3308100 3308101 3309101 3309102 3310101 3315102 3401102
3401105 3401124 3402103 3404101 3404109 3404115 3404117 3404122 3404123 3404124 3404128 3404129 3404130
3404131 3404201 3404202 3404307 3404405 3404450 3405102 3480101 3480102 3480103 3494101 3494102 3500111
3500303 3500304 3500305 3501120 3501214 3501217 3501222 3501224 3502222 3502271 3502272 3503101 3503111
3600202 3600204 3600205 3600206 3600207 3600208 3600209 3600301 3600302 3600303 3600304 3600305 3600306
3640203 3700104 3700105 3700107 3700108 3700109 3700110 3700113 3700114 3700115 3705102 3741101 3741102
3741107 3741108 3741207 3741208 3741329 3741341 3741426 3742100 3742102 3742106 3742107 3743422 3800102
3800105 3800130 3800202 3800250 3800251 3800252 3800260 3800309 3800351 3900200 3900202 3900203 3900204
3900205 3900206 3900207 3914101 4000101 4000102 4000104 4000105 4000203 4000204 4000205 4000206 4000208
4000209 4000210 4000215 5100101 5600124 5600125 5600160 5600161 5600170 5600171 5600180 5600181 5600182
5600183 5600184 5600185 5600186 5600187 5600188 5600190 5600191 5695101 5695102 5698101
""".split()

COURSE_Q = """query($courseNo: String!, $courseGroup: CourseGroupInput!) {
  course(courseNo: $courseNo, courseGroup: $courseGroup) {
    semester academicYear courseNo abbrName courseNameTh courseNameEn faculty department
    credit creditHours courseCondition courseDescTh courseDescEn genEdType
    midterm { date period { start end } }  final { date period { start end } }
    sections { genEdType sectionNo closed capacity { current max } note
      classes { type dayOfWeek period { start end } room building teachers } } } }"""
REVIEWS_Q = """query($courseNo: String!, $studyProgram: StudyProgram!) {
  reviews(courseNo: $courseNo, studyProgram: $studyProgram) {
    rating content likeCount semester academicYear } }"""

GENED = {"SO": "Social Science (สังคมศาสตร์)", "HU": "Humanities (มนุษยศาสตร์)",
         "SC": "Science and Mathematics (วิทยาศาสตร์และคณิตศาสตร์)", "IN": "Interdisciplinary (สหศาสตร์)",
         "NO": "Not GenEd"}
DAY = {"MO": "Mon จันทร์", "TU": "Tue อังคาร", "WE": "Wed พุธ", "TH": "Thu พฤหัสบดี",
       "FR": "Fri ศุกร์", "SA": "Sat เสาร์", "SU": "Sun อาทิตย์"}


def gql(query, variables):
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                time.sleep(DELAY)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 403:  # blocked: retrying won't help, stop and show why
                sys.exit(f"HTTP 403 from {API}. Check USER_AGENT at the top of this file.\n"
                         + e.read()[:300].decode("utf-8", "replace"))
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def fetch(no):
    cache = RAW / f"{no}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    course = None
    for year, sem in TERMS:
        res = gql(COURSE_Q, {"courseNo": no, "courseGroup": {"academicYear": year, "semester": sem,
                                                             "studyProgram": "S"}})
        course = (res.get("data") or {}).get("course")
        if course:
            break
    if course:
        res = gql(REVIEWS_Q, {"courseNo": no, "studyProgram": "S"})
        course["reviews"] = (res.get("data") or {}).get("reviews") or []
        cache.write_text(json.dumps(course, ensure_ascii=False, indent=1), encoding="utf-8")
    return course


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def exam(e):
    if not e or not e.get("date"):
        return "-"
    p = e.get("period") or {}
    return f"{e['date'][:10]} {p.get('start', '')}-{p.get('end', '')}"


def to_markdown(c):
    no = c["courseNo"]
    gtypes = sorted({c.get("genEdType")} | {s.get("genEdType") for s in c.get("sections") or []} - {None})
    if len(gtypes) > 1 and "NO" in gtypes:
        gtypes.remove("NO")
    ratings = [r["rating"] / 2 for r in c.get("reviews") or [] if r.get("rating")]  # API 1-10 -> /5
    days, teachers = [], set()
    for s in c.get("sections") or []:
        for cl in s.get("classes") or []:
            if cl.get("dayOfWeek") in DAY and cl["dayOfWeek"] not in days:
                days.append(cl["dayOfWeek"])
            teachers.update(t for t in cl.get("teachers") or [] if t)
    days = [d for d in DAY if d in days]

    md = [f"# {no} {c.get('abbrName') or ''} — {clean(c.get('courseNameEn'))} ({clean(c.get('courseNameTh'))})", "",
          f"- **Course number:** {no}",
          f"- **GenEd category:** {', '.join(GENED.get(g, g) for g in gtypes) or '-'}",
          f"- **Credits:** {c.get('credit')} ({c.get('creditHours') or '-'})",
          f"- **Faculty / Department code:** {c.get('faculty') or '-'} / {c.get('department') or '-'}",
          f"- **Term:** {c.get('academicYear')}/{c.get('semester')}",
          f"- **Class days:** {', '.join(DAY[d] for d in days) or '-'}",
          f"- **Instructors:** {', '.join(sorted(teachers)) or '-'}",
          f"- **Midterm:** {exam(c.get('midterm'))} | **Final:** {exam(c.get('final'))}",
          f"- **Student rating:** " + (f"{sum(ratings) / len(ratings):.2f}/5 from {len(ratings)} reviews"
                                       if ratings else "no reviews"),
          f"- **Link:** https://cugetreg.com/S/courses/{no}", ""]
    if clean(c.get("courseCondition")).strip("-– "):
        md += ["## Prerequisites / เงื่อนไขรายวิชา", clean(c["courseCondition"]), ""]
    if clean(c.get("courseDescEn")):
        md += ["## Description", clean(c["courseDescEn"]), ""]
    if clean(c.get("courseDescTh")):
        md += ["## คำอธิบายรายวิชา", clean(c["courseDescTh"]), ""]
    secs = c.get("sections") or []
    if secs:
        md += ["## Sections", "", "| Sec | Day & time | Room | Instructors | Seats | Status |",
               "|---|---|---|---|---|---|"]
        for s in secs:
            cls = s.get("classes") or [{}]
            when = "; ".join(f"{cl.get('dayOfWeek') or '-'} {(cl.get('period') or {}).get('start', '')}-"
                             f"{(cl.get('period') or {}).get('end', '')}" for cl in cls)
            room = "; ".join(f"{cl.get('building') or ''} {cl.get('room') or ''}".strip() or "-" for cl in cls)
            tch = ", ".join(sorted({t for cl in cls for t in cl.get("teachers") or [] if t})) or "-"
            cap = s.get("capacity") or {}
            md.append(f"| {s.get('sectionNo')} | {when} | {room} | {tch} | {cap.get('current', '-')}/"
                      f"{cap.get('max', '-')} | {'closed' if s.get('closed') else 'open'} |")
        md.append("")
    revs = [r for r in c.get("reviews") or [] if clean(r.get("content"))]
    if revs:
        md += ["## Student reviews / รีวิวจากนิสิต"]
        for r in sorted(revs, key=lambda r: -(r.get("likeCount") or 0)):
            md.append(f"- ({r['rating'] / 2:g}/5, {r.get('academicYear')}/{r.get('semester')}) "
                      f"{clean(r['content'])}")
        md.append("")
    return "\n".join(md)


def main():
    nos = sys.argv[1:] or COURSES
    RAW.mkdir(exist_ok=True); MD_DIR.mkdir(exist_ok=True)
    done, missing = [], []
    for i, no in enumerate(nos, 1):
        try:
            c = fetch(no)
        except Exception as e:
            print(f"[{i}/{len(nos)}] {no}: ERROR {e} (rerun later to retry)")
            missing.append(no)
            continue
        if not c:
            print(f"[{i}/{len(nos)}] {no}: not found in any recent term")
            missing.append(no)
            continue
        md = to_markdown(c)
        (MD_DIR / f"{no}.md").write_text(md, encoding="utf-8")
        done.append(md)
        print(f"[{i}/{len(nos)}] {no} {c.get('abbrName')} ok")
    Path("courses.md").write_text("\n\n---\n\n".join(done) + "\n", encoding="utf-8")
    print(f"\n{len(done)} courses -> courses_md/ and courses.md")
    if missing:
        Path("missing.txt").write_text("\n".join(missing) + "\n")
        print(f"{len(missing)} missing -> missing.txt")


if __name__ == "__main__":
    main()