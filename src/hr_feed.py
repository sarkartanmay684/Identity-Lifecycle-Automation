"""Validate the lab CSV and group mapping without accessing Entra."""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path


COLUMNS = (
    "employee_id", "first_name", "last_name", "department", "job_title",
    "manager_upn", "start_date", "end_date", "status",
)


class InputError(ValueError):
    """An input file is invalid; no directory plan should be emitted."""


@dataclass(frozen=True)
class Employee:
    employee_id: str
    first_name: str
    last_name: str
    department: str
    job_title: str
    manager_upn: str
    start_date: date
    end_date: date | None
    status: str


@dataclass(frozen=True)
class GroupMap:
    employee_id_prefix: str
    common_groups: tuple[str, ...]
    department_groups: dict[str, tuple[str, ...]]

    def groups_for(self, department: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.common_groups + self.department_groups[department]))


def iso_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("use YYYY-MM-DD")
    return date.fromisoformat(value)


def username_base(first_name: str, last_name: str) -> str:
    def component(name: str) -> str:
        normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", normalized.lower())

    first, last = component(first_name), component(last_name)
    if not first or not last:
        raise ValueError("the lab naming rule needs an ASCII letter or digit in each name")
    return (first + "." + last)[:64].rstrip(".")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


def load_group_map(path: str | Path) -> GroupMap:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise InputError("Cannot read group mapping as UTF-8 JSON. Check the file path and JSON syntax.") from None
    required = {"employee_id_prefix", "common_groups", "department_groups"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise InputError("Group mapping must contain employee_id_prefix, common_groups, and department_groups only.")
    prefix = raw["employee_id_prefix"]
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,14}", prefix):
        raise InputError("employee_id_prefix must be 2-15 ASCII letters, digits, underscores, or hyphens.")

    def group_names(value, label):
        if not isinstance(value, list) or not value or any(
            not isinstance(name, str) or not name.strip() or name != name.strip()
            or any(ord(char) < 32 for char in name) for name in value
        ):
            raise InputError(f"{label} must be a non-empty list of clean group display names.")
        if len({name.casefold() for name in value}) != len(value):
            raise InputError(f"{label} contains duplicate group names.")
        return tuple(value)

    common = group_names(raw["common_groups"], "common_groups")
    departments = raw["department_groups"]
    if not isinstance(departments, dict) or not departments:
        raise InputError("department_groups must be a non-empty JSON object.")
    cleaned = {}
    for department, names in departments.items():
        if not department or department != department.strip() or len(department) > 64:
            raise InputError("Department keys must be clean names of at most 64 characters.")
        if department.casefold() in {name.casefold() for name in cleaned}:
            raise InputError("Department keys must not differ only by letter case.")
        cleaned[department] = group_names(names, f"department_groups.{department}")
    return GroupMap(prefix, common, cleaned)


def load_feed(path: str | Path, mapping: GroupMap) -> list[Employee]:
    employees, errors, seen = [], [], set()
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames
            if headers is None or len(headers) != len(COLUMNS) or set(headers) != set(COLUMNS):
                raise InputError("CSV must have exactly these nine headers: " + ",".join(COLUMNS))
            for raw in reader:
                line = reader.line_num
                if None in raw or any(value is None for value in raw.values()):
                    errors.append(f"Row ending at line {line}: wrong number of CSV fields.")
                    continue
                row = {key: value.strip() for key, value in raw.items()}
                problems = []
                for key in COLUMNS:
                    if key != "end_date" and not row[key]:
                        problems.append(f"{key} is required")
                    if any(ord(char) < 32 or ord(char) == 127 for char in row[key]):
                        problems.append(f"{key} contains a control character")
                employee_id = row["employee_id"]
                if not employee_id.startswith(mapping.employee_id_prefix) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}", employee_id
                ):
                    problems.append("employee_id must use the configured prefix and be at most 16 ASCII characters")
                if employee_id.casefold() in seen:
                    problems.append("duplicate employee_id")
                seen.add(employee_id.casefold())
                for key, limit in (("first_name", 64), ("last_name", 64), ("department", 64), ("job_title", 128)):
                    if len(row[key]) > limit:
                        problems.append(f"{key} exceeds {limit} characters")
                if row["department"] not in mapping.department_groups:
                    problems.append("department is not in group_map.json")
                if row["status"] not in {"active", "transferred", "terminated"}:
                    problems.append("status must be active, transferred, or terminated")
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", row["manager_upn"]):
                    problems.append("manager_upn must be a user principal name")
                try:
                    start = iso_date(row["start_date"])
                except ValueError:
                    start = None
                    problems.append("start_date must be a real date in YYYY-MM-DD format")
                end = None
                if row["end_date"]:
                    try:
                        end = iso_date(row["end_date"])
                    except ValueError:
                        problems.append("end_date must be a real date in YYYY-MM-DD format")
                if row["status"] == "terminated" and not row["end_date"]:
                    problems.append("terminated rows require end_date")
                if row["status"] in {"active", "transferred"} and row["end_date"]:
                    problems.append("active and transferred rows must have a blank end_date")
                if start and end and end < start:
                    problems.append("end_date cannot precede start_date")
                if problems:
                    errors.extend(f"Row ending at line {line}: {problem}." for problem in problems)
                else:
                    employees.append(Employee(
                        employee_id, row["first_name"], row["last_name"], row["department"],
                        row["job_title"], row["manager_upn"].lower(), start, end, row["status"],
                    ))
    except (OSError, UnicodeError, csv.Error):
        raise InputError("Cannot read the CSV. Use UTF-8, commas, and matching quotes; check the path.") from None
    if errors:
        raise InputError("CSV validation failed:\n" + "\n".join(errors))
    if not employees:
        raise InputError("The CSV contains no employees. No lifecycle actions are inferred from an empty feed.")
    return employees
