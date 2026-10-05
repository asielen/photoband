"""{names:rows}: how faces are grouped into rows, and a photo's own row count."""
import datetime as dt

from captiontokens import Face, cluster_rows, resolve
from captiontokens.tokens import face_row_groups

T = dt.date(2026, 9, 30)


def face(name, cx, cy, w=0.036, h=0.049):
    return {"name": name, "box": [cx - w / 2, cy - h / 2, w, h]}


# 210412-bag-woodbury-034.tif as read: six people standing by a car (heads at different heights,
# some leaning), one man crouching in front
STANDING_BY_A_CAR = [
    face("Frances", 0.505, 0.342), face("Carl", 0.222, 0.373), face("Irma", 0.359, 0.380),
    face("Joseph", 0.744, 0.416), face("Florence", 0.675, 0.421), face("Clara", 0.612, 0.429),
    face("Oliver", 0.408, 0.504),
]


def rows_of(faces, **kw):
    return [[f.name for f in r] for r in cluster_rows([Face(f["name"], f["box"]) for f in faces], **kw)]


def test_people_standing_at_different_heights_are_one_row():
    assert rows_of(STANDING_BY_A_CAR) == [["Oliver"], ["Carl", "Irma", "Frances", "Clara", "Florence", "Joseph"]]
    assert resolve("{names:rows}", {"faces": STANDING_BY_A_CAR}, T).text == (
        "Front row, L–R: Oliver; Back row, L–R: Carl, Irma, Frances, Clara, Florence and Joseph")


def test_staggered_rows_less_than_a_face_apart_stay_apart():
    # a back row standing in the gaps of the front row, 0.8 face heights higher
    front = [face(f"F{i}", 0.1 + i * 0.16, 0.60 + (i % 2) * 0.005) for i in range(5)]
    back = [face(f"B{i}", 0.18 + i * 0.16, 0.56 + (i % 2) * 0.005) for i in range(4)]
    assert rows_of(front + back) == [[f"F{i}" for i in range(5)], [f"B{i}" for i in range(4)]]


def test_three_tiers():
    faces = [face(f"{r}{i}", 0.1 + i * 0.1, y) for r, y in (("A", 0.7), ("B", 0.62), ("C", 0.54)) for i in range(6)]
    assert [len(r) for r in rows_of(faces)] == [6, 6, 6]


def test_a_photos_own_row_count():
    assert rows_of(STANDING_BY_A_CAR, rows=1) == [["Carl", "Irma", "Oliver", "Frances", "Clara", "Florence", "Joseph"]]
    assert [len(r) for r in rows_of(STANDING_BY_A_CAR, rows=3)] == [1, 3, 3]
    assert rows_of([face("A", 0.5, 0.5)], rows=4) == [["A"]]   # never more rows than people
    one_row = {"faces": STANDING_BY_A_CAR, "face_rows": 1}
    assert resolve("{names:rows}", one_row, T).text == "Carl, Irma, Oliver, Frances, Clara, Florence and Joseph"
    for bad in (0, 10, "x", True, None, 2.5):
        assert resolve("{names:rows}", {"faces": STANDING_BY_A_CAR, "face_rows": bad}, T).text == \
            resolve("{names:rows}", {"faces": STANDING_BY_A_CAR}, T).text


def test_groups_for_the_people_list():
    assert face_row_groups({"faces": STANDING_BY_A_CAR}) == [[6], [1, 2, 0, 5, 4, 3]]
    assert face_row_groups({"faces": STANDING_BY_A_CAR, "face_rows": 1}) == [[1, 2, 6, 0, 5, 4, 3]]
    # someone without a place on the photo: {names:rows} prints plain names, so no rows either
    assert face_row_groups({"faces": STANDING_BY_A_CAR + [{"name": "Somebody", "box": None}]}) == []
    assert face_row_groups({"faces": [{"name": "X", "box": None}]}) == []
    assert face_row_groups({}) == []
