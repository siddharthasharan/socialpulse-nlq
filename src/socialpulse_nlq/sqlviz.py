"""Deterministic SQL visualization for SocialPulse, no LLM.

Two independent pieces, both derived from the executed query only:

1. Chart + table of the query's result rows. The chart is a heuristic
   over the result shape, not a claim about the metric: a two-column
   result whose first column looks like a date/month and whose second
   column is numeric draws a line chart; a two-column result whose
   first column is categorical and whose second column is numeric
   draws a bar chart; anything else gets the table only, with a note
   saying why no chart was drawn. The table is always present.
2. Diagram of the SQL itself: source tables -> joins -> filters ->
   grouping/aggregation -> output, as stage boxes carrying the actual
   table names, join conditions, filter conditions, and aggregation
   functions from the SQL text.

Strictness is the point. The parser only models flat SELECTs and
one level of subquery wrapping (``SELECT AGG(x) FROM (flat query)``),
because those are the shapes the planner templates and chat helpers
actually produce. Anything else — WITH/CTE, UNION, a scalar subquery
in the SELECT list, a multi-subquery FROM (USING joins), EXISTS in
the SELECT list — returns ``diagram: None`` with a reason, never a
guessed diagram. Chart availability is independent of diagram
availability: a UNION query can still chart its rows.

Never diagram or chart the raw-implied SQL of an aggregate-routed
turn. Callers pass only the SQL that actually ran; the raw SQL stays
labelled NOT executed in the working panel, as before.

Result previews are capped at PREVIEW_LIMIT rows; the payload says
so when capped. No new query execution happens here: callers pass
rows they already ran.
"""
import re

PREVIEW_LIMIT = 50

_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_AGG_RE = re.compile(
    r"(COUNT|SUM|AVG|MEDIAN|MIN|MAX)\s*\(\s*(DISTINCT\s+)?([^)]+?)\)", re.I)


def _strip_comments(sql):
    return re.sub(r"--[^\n]*", "", sql or "")


def _find_top_level(sql, keyword):
    """Index of keyword at paren depth 0 (word-boundary), or -1."""
    depth = 0
    i = 0
    kw = keyword.upper()
    in_str = False
    while i < len(sql):
        ch = sql[i]
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and sql[i:i + len(kw)].upper() == kw:
            before = sql[i - 1] if i > 0 else " "
            after = sql[i + len(kw)] if i + len(kw) < len(sql) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return i
        i += 1
    return -1


def _split_top_level(text, sep="AND"):
    """Split on a top-level separator (paren depth 0, outside strings)."""
    parts, depth, cur, in_str = [], 0, [], False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            cur.append(ch)
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif depth == 0 and text[i:i + len(sep)].upper() == sep:
            # Word separators (AND) need word boundaries so they do not
            # fire inside identifiers; punctuation (",") does not.
            if sep.isalpha():
                before = text[i - 1] if i > 0 else " "
                after = text[i + len(sep)] if i + len(sep) < len(text) else " "
                if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
                    cur.append(ch)
                    i += 1
                    continue
            parts.append("".join(cur))
            cur = []
            i += len(sep)
            continue
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _split_commas(text):
    return _split_top_level(text, sep=",")


def _matching_paren(sql, open_idx):
    depth = 0
    in_str = False
    for i in range(open_idx, len(sql)):
        ch = sql[i]
        if in_str:
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _clean(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _parse_select_list(select_text):
    """Return (aggregations, output_columns) or None if a scalar
    subquery appears in the SELECT list (we do not model those)."""
    if re.search(r"\(\s*SELECT\b", select_text, re.I):
        return None
    aggs, cols = [], []
    for item in _split_commas(select_text):
        m = _AGG_RE.search(item)
        alias = ""
        # Alias is the trailing "AS name" only: an expression may itself
        # contain AS (CAST(x AS DATE)), which must not be read as one.
        am = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", item, re.I)
        if am:
            alias = am.group(1)
        if m:
            aggs.append({"function": m.group(1).upper(),
                         "distinct": bool(m.group(2)),
                         "expression": _clean(m.group(3)),
                         "alias": alias})
        else:
            cols.append(_clean(item))
    return aggs, cols


def _parse_from_clause(from_text):
    """Parse a FROM clause. Returns (tables, joins, subquery_text) or
    None when the shape is not modelled. tables are (name, alias)."""
    text = _clean(from_text)
    # A FROM clause that is a single parenthesised subquery (optionally
    # aliased) is handled by the caller, not here.
    if text.startswith("("):
        return None
    # Split off JOIN segments at top level.
    join_idx = _find_top_level(text, "JOIN")
    base = (text if join_idx == -1 else text[:join_idx]).strip()
    rest = "" if join_idx == -1 else text[join_idx:].strip()
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?$", base)
    if not m:
        return None
    tables = [(m.group(1), m.group(2) or "")]
    joins = []
    while rest:
        jm = re.match(r"JOIN\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?\s+ON\s+(.*)$",
                      rest, re.I | re.S)
        if not jm:
            return None
        tables.append((jm.group(1), jm.group(2) or ""))
        on_text = jm.group(3)
        nxt = _find_top_level(on_text, "JOIN")
        cond = on_text if nxt == -1 else on_text[:nxt]
        joins.append({"table": jm.group(1), "alias": jm.group(2) or "",
                      "on": _clean(cond)})
        rest = "" if nxt == -1 else on_text[nxt:]
    return tables, joins, None


def _parse_flat(sql):
    """Parse one flat SELECT. Returns a diagram dict, or
    (None, reason) when the shape is not modelled."""
    s = _clean(_strip_comments(sql))
    if re.search(r"^WITH\b", s, re.I):
        return None, "query uses WITH/CTE, which the diagram does not model"
    if not s.upper().startswith("SELECT "):
        return None, "query does not start with SELECT"
    if _find_top_level(s, "UNION") != -1:
        return None, "query uses UNION, which the diagram does not model"
    from_idx = _find_top_level(s, "FROM")
    if from_idx == -1:
        return None, "query has no FROM clause"
    select_text = s[len("SELECT "):from_idx]
    parsed_select = _parse_select_list(select_text)
    if parsed_select is None:
        return None, "query has a subquery in its SELECT list, which the diagram does not model"
    aggs, out_cols = parsed_select
    rest = s[from_idx + len("FROM"):]
    # Clause boundaries, in order, at top level.
    bounds = {}
    for kw in ("WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT"):
        idx = _find_top_level(rest, kw)
        if idx != -1:
            bounds[kw] = idx
    def _clause(name, nxt):
        if name not in bounds:
            return ""
        start = bounds[name] + len(name)
        ends = [v for k, v in bounds.items() if v > bounds[name]]
        end = min(ends) if ends else len(rest)
        return rest[start:end]
    from_text = rest[:min(bounds.values())] if bounds else rest
    # Subquery-wrapped FROM is handled by parse_sql, not here.
    if _clean(from_text).startswith("("):
        return None, "subquery in FROM (handled only by the wrapper path)"
    parsed_from = _parse_from_clause(from_text)
    if parsed_from is None:
        return None, "FROM clause shape is not modelled (expected table [JOIN table ON ...])"
    tables, joins, _ = parsed_from
    where_text = _clause("WHERE", None)
    filters = []
    if where_text:
        # Protect the AND inside BETWEEN ... AND ... so it is not
        # split into two bogus conditions.
        protected = re.sub(
            r"(BETWEEN\s+(?:DATE\s+'[^']+'|\S+)\s+)AND",
            lambda m: m.group(1) + "__BETWEEN_AND__",
            where_text, flags=re.I)
        filters = [c.replace("__BETWEEN_AND__", "AND")
                   for c in _split_top_level(protected, "AND")
                   if _clean(c) != "1=1"]
        if any(re.search(r"\(\s*SELECT\b", c, re.I) for c in filters):
            return None, "query has a subquery in its WHERE clause, which the diagram does not model"
    group_text = _clause("GROUP BY", None)
    group_by = _split_commas(group_text) if group_text else []
    stages = [{"stage": "sources",
               "items": [t + (f" AS {a}" if a else "") for t, a in tables]}]
    if joins:
        stages.append({"stage": "joins",
                       "items": [f"{j['table']} ON {j['on']}" for j in joins]})
    if filters:
        stages.append({"stage": "filters", "items": filters})
    if group_by:
        stages.append({"stage": "grouping", "items": group_by})
    if aggs:
        stages.append({"stage": "aggregation",
                       "items": [f"{a['function']}({('DISTINCT ' if a['distinct'] else '')}{a['expression']})"
                                 + (f" AS {a['alias']}" if a["alias"] else "")
                                 for a in aggs]})
    stages.append({"stage": "output",
                   "items": out_cols if out_cols else
                   [a["alias"] or a["expression"] for a in aggs]})
    return {"stages": stages, "tables": [t for t, _ in tables],
            "subquery": False}, None


def parse_sql(sql):
    """Parse SQL into a diagram structure.

    Returns (diagram_dict, None) on success, or (None, reason) when
    the shape is not modelled. Never guesses: an unmodelled shape
    yields a reason, not a partial diagram.
    """
    if not sql or not isinstance(sql, str):
        return None, "no SQL to diagram"
    s = _clean(_strip_comments(sql))
    # One level of subquery wrapping: SELECT ... FROM (flat query)
    # [GROUP BY ...] [ORDER BY ...]. The inner query supplies the
    # stages; the outer SELECT/GROUP BY supplies the final aggregation.
    from_idx = _find_top_level(s, "FROM")
    if from_idx != -1:
        rest = s[from_idx + len("FROM"):].lstrip()
        if rest.startswith("("):
            open_idx = s.index("(", from_idx)
            close_idx = _matching_paren(s, open_idx)
            if close_idx == -1:
                return None, "unbalanced parentheses in subquery"
            inner = s[open_idx + 1:close_idx]
            after = s[close_idx + 1:].strip()
            # After the subquery there may be an alias and outer
            # GROUP BY / ORDER BY / LIMIT only. A second subquery,
            # a JOIN, or a USING join is not modelled.
            if re.search(r"\bJOIN\b", after, re.I) or re.search(r"\bUSING\b", after, re.I):
                return None, "query joins subquery results, which the diagram does not model"
            if re.search(r"\(\s*SELECT\b", after, re.I):
                return None, "query has a second subquery, which the diagram does not model"
            inner_diagram, reason = parse_sql(inner)
            if inner_diagram is None:
                return None, reason
            outer_select = s[len("SELECT "):from_idx]
            parsed_select = _parse_select_list(outer_select)
            if parsed_select is None:
                return None, "query has a subquery in its SELECT list, which the diagram does not model"
            outer_aggs, outer_cols = parsed_select
            group_idx = _find_top_level(after, "GROUP BY")
            outer_group = _split_commas(after[group_idx + len("GROUP BY"):]) if group_idx != -1 else []
            # Drop the inner output stage; the outer stages replace it.
            stages = [st for st in inner_diagram["stages"] if st["stage"] != "output"]
            if outer_group:
                stages.append({"stage": "grouping", "items": outer_group})
            if outer_aggs:
                stages.append({"stage": "aggregation",
                               "items": [f"{a['function']}({('DISTINCT ' if a['distinct'] else '')}{a['expression']})"
                                         + (f" AS {a['alias']}" if a["alias"] else "")
                                         for a in outer_aggs]})
            stages.append({"stage": "output",
                           "items": outer_cols if outer_cols else
                           [a["alias"] or a["expression"] for a in outer_aggs]})
            return {"stages": stages, "tables": inner_diagram["tables"],
                    "subquery": True}, None
    return _parse_flat(s)


def _json_value(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def _chart_for(columns, rows):
    """Chart heuristic, or None.

    Charts the first two columns when the second is numeric for every
    row: line if the first column looks like a date/month, bar if it
    is categorical. Further columns (e.g. breakdown share, monthly
    creators) stay in the table only. Anything else — a single scalar,
    or a (category, date, value) result whose second column is a date
    (one bar per row would repeat categories) — gets the table only.
    """
    if len(columns) < 2 or not rows:
        return None
    y_vals = []
    for r in rows:
        if len(r) < 2 or not isinstance(r[1], (int, float)) or isinstance(r[1], bool):
            return None
        y_vals.append(r[1])
    x_vals = [str(r[0]) for r in rows]
    kind = "line" if all(_DATE_RE.match(x) for x in x_vals) else "bar"
    return {"kind": kind, "x_label": columns[0], "y_label": columns[1],
            "points": [{"x": x, "y": y} for x, y in zip(x_vals, y_vals)]}


def build_visualization(sql, columns, rows, executed=True):
    """Build the visualization payload for one executed query.

    sql must be the SQL that actually ran. Pass executed=False only
    to get an explicit refusal payload (callers should instead pass
    the executed SQL and label any raw SQL separately).
    """
    if not executed:
        return {"available": False,
                "reason": "This SQL was not executed, so it has no result to visualize.",
                "columns": [], "rows": [], "row_count": 0, "capped": False,
                "chart": None, "diagram": None,
                "diagram_unavailable_reason": "SQL was not executed"}
    columns = list(columns or [])
    clean_rows = [[_json_value(v) for v in r] for r in (rows or [])]
    diagram, diagram_reason = parse_sql(sql) if sql else (None, "no SQL to diagram")
    chart = _chart_for(columns, clean_rows)
    chart_note = None
    if chart is None and clean_rows:
        if len(columns) < 2:
            chart_note = ("No chart drawn: this result has a single column, "
                          "so the table below is the visualization.")
        elif not all(isinstance(r[1], (int, float)) and not isinstance(r[1], bool)
                     for r in clean_rows if len(r) > 1):
            chart_note = ("No chart drawn: the second column is not numeric "
                          "(here it is a date/category per row), "
                          "so the table below is the visualization.")
        else:
            chart_note = ("No chart drawn for this result shape, "
                          "so the table below is the visualization.")
    capped = len(clean_rows) > PREVIEW_LIMIT
    return {"available": True, "columns": columns,
            "rows": clean_rows[:PREVIEW_LIMIT],
            "row_count": len(clean_rows), "capped": capped,
            "chart": chart, "chart_note": chart_note,
            "diagram": diagram, "diagram_unavailable_reason": diagram_reason}
