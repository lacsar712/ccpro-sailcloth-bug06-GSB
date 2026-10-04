from django.test import TestCase
from rest_framework.test import APIClient

from .models import ClothRoll, Loft


class GsmFilterTests(TestCase):
    """架面筛选：挂签显隐、架底计数必须等于库里 gsm>400 的卷数。"""

    def setUp(self):
        self.client = APIClient()
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="w", password="x")
        self.client.force_authenticate(user)
        self.loft = Loft.objects.create(name="间一")
        # 380 原布：正是容易被「raw 旁路」夹进挂签的轻卷
        ClothRoll.objects.create(
            loft=self.loft, roll_code="L", status=ClothRoll.STATUS_RAW,
            fabric_weight_gsm=380,
        )
        ClothRoll.objects.create(
            loft=self.loft, roll_code="H1", status=ClothRoll.STATUS_DIPPING,
            fabric_weight_gsm=420,
        )
        ClothRoll.objects.create(
            loft=self.loft, roll_code="H2", status=ClothRoll.STATUS_CURED,
            fabric_weight_gsm=450,
        )
        # 恰好 400：严格大于，不算
        ClothRoll.objects.create(
            loft=self.loft, roll_code="E", status=ClothRoll.STATUS_CURED,
            fabric_weight_gsm=400,
        )

    def test_filter_strict_greater_than_ignores_raw_light_rolls(self):
        db_heavy = set(
            ClothRoll.objects.filter(fabric_weight_gsm__gt=400).values_list(
                "roll_code", flat=True
            )
        )
        resp = self.client.get("/api/rolls/", {"gsmMin": 400})
        codes = {r["rollCode"] for r in resp.data["results"]}
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(codes, db_heavy)
        self.assertEqual(codes, {"H1", "H2"})
        self.assertNotIn("L", codes)

    def test_filter_count_matches_database(self):
        """点亮条数（接口条数）= 架底计数 = 库里 >400 的卷数。"""
        db_count = ClothRoll.objects.filter(fabric_weight_gsm__gt=400).count()
        resp = self.client.get("/api/rolls/", {"gsmMin": 400})
        self.assertEqual(len(resp.data["results"]), db_count)
        self.assertEqual(db_count, 2)


class OptimisticLockTests(TestCase):
    """两名浸胶工交叉改同一卷克重：只许一版写入，输家得 409。"""

    def setUp(self):
        self.client = APIClient()
        from django.contrib.auth import get_user_model

        u1 = get_user_model().objects.create_user(username="w1", password="x")
        u2 = get_user_model().objects.create_user(username="w2", password="x")
        self.c1 = APIClient()
        self.c1.force_authenticate(u1)
        self.c2 = APIClient()
        self.c2.force_authenticate(u2)
        self.roll = ClothRoll.objects.create(
            loft=Loft.objects.create(name="间"),
            roll_code="R-1",
            fabric_weight_gsm=380,
        )

    def test_second_writer_gets_409_and_first_version_survives(self):
        # 两人都基于 version=1 开卷
        r1 = self.c1.patch(f"/api/rolls/{self.roll.id}/",
                           {"fabricWeightGsm": 420, "version": 1})
        r2 = self.c2.patch(f"/api/rolls/{self.roll.id}/",
                           {"fabricWeightGsm": 460, "version": 1})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 409)

        self.roll.refresh_from_db()
        self.assertEqual(self.roll.fabric_weight_gsm, 420)  # 只有一版留下
        self.assertEqual(self.roll.version, 2)

        # 输家刷新后带新版本重试，写入成功
        r3 = self.c2.patch(f"/api/rolls/{self.roll.id}/",
                           {"fabricWeightGsm": 460, "version": 2})
        self.assertEqual(r3.status_code, 200)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.fabric_weight_gsm, 460)
        self.assertEqual(self.roll.version, 3)

    def test_after_conflict_filter_and_count_follow_database(self):
        self.c1.patch(f"/api/rolls/{self.roll.id}/",
                      {"fabricWeightGsm": 420, "version": 1})
        lost = self.c2.patch(f"/api/rolls/{self.roll.id}/",
                             {"fabricWeightGsm": 300, "version": 1})
        self.assertEqual(lost.status_code, 409)

        db_heavy = ClothRoll.objects.filter(fabric_weight_gsm__gt=400).count()
        resp = self.client_get_heavy()
        self.assertEqual(len(resp.data["results"]), db_heavy)
        self.assertEqual(db_heavy, 1)

    def client_get_heavy(self):
        return self.c1.get("/api/rolls/", {"gsmMin": 400})
