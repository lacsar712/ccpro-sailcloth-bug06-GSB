from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import ClothRoll, Loft


class GsmFilterTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="worker", password="x")
        self.client.force_authenticate(self.user)
        self.loft_a = Loft.objects.create(name="甲帆布间")
        self.loft_b = Loft.objects.create(name="乙帆布间")
        # 380 原布卷：旧 bug 下会借 raw 身份漏进 >400 筛选
        ClothRoll.objects.create(
            loft=self.loft_a, roll_code="R380-raw",
            status=ClothRoll.STATUS_RAW, fabric_weight_gsm=380,
        )
        ClothRoll.objects.create(
            loft=self.loft_a, roll_code="R400-dip",
            status=ClothRoll.STATUS_DIPPING, fabric_weight_gsm=400,
        )
        ClothRoll.objects.create(
            loft=self.loft_a, roll_code="R420-cured",
            status=ClothRoll.STATUS_CURED, fabric_weight_gsm=420,
        )
        ClothRoll.objects.create(
            loft=self.loft_b, roll_code="R500-raw",
            status=ClothRoll.STATUS_RAW, fabric_weight_gsm=500,
        )

    def test_gsm_min_is_strict_gt_and_matches_db_count(self):
        """gsmMin=400 必须严格大于 400，且条数等于库里的卷数（跨帆布间）。"""
        db_count = ClothRoll.objects.filter(fabric_weight_gsm__gt=400).count()
        self.assertEqual(db_count, 2)

        resp = self.client.get("/api/rolls/?gsmMin=400")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data["results"] if "results" in resp.data else resp.data
        self.assertEqual(len(rows), db_count)
        self.assertEqual({r["rollCode"] for r in rows}, {"R420-cured", "R500-raw"})
        for r in rows:
            self.assertGreater(r["fabricWeightGsm"], 400)

    def test_heavy_filter_combined_with_loft(self):
        resp = self.client.get(f"/api/rolls/?gsmMin=400&loftId={self.loft_a.id}")
        rows = resp.data["results"] if "results" in resp.data else resp.data
        self.assertEqual([r["rollCode"] for r in rows], ["R420-cured"])


class ConcurrentWeightEditTests(APITestCase):
    def setUp(self):
        self.worker_a = User.objects.create_user(username="dipper_a", password="x")
        self.worker_b = User.objects.create_user(username="dipper_b", password="x")
        self.loft = Loft.objects.create(name="甲帆布间")
        self.roll = ClothRoll.objects.create(
            loft=self.loft, roll_code="R-X",
            status=ClothRoll.STATUS_RAW, fabric_weight_gsm=380,
        )

    def _as(self, user):
        self.client.force_authenticate(user)

    def test_cross_edit_leaves_only_one_version(self):
        """两名浸胶工交叉改同一卷：先提交的成，后提交的 409，库里只留一版。"""
        # 两人各拿到一版（version=0）
        self._as(self.worker_a)
        a_read = self.client.get(f"/api/rolls/{self.roll.id}/").data
        self._as(self.worker_b)
        b_read = self.client.get(f"/api/rolls/{self.roll.id}/").data
        self.assertEqual(a_read["version"], 0)
        self.assertEqual(b_read["version"], 0)

        # A 先改成 430
        resp_a = self.client.patch(
            f"/api/rolls/{self.roll.id}/",
            {"fabricWeightGsm": 430, "version": a_read["version"]},
            format="json",
        )
        self.assertEqual(resp_a.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_a.data["version"], 1)

        # B 拿着过期版本改成 450：必须被拒
        resp_b = self.client.patch(
            f"/api/rolls/{self.roll.id}/",
            {"fabricWeightGsm": 450, "version": b_read["version"]},
            format="json",
        )
        self.assertEqual(resp_b.status_code, status.HTTP_409_CONFLICT)

        self.roll.refresh_from_db()
        self.assertEqual(self.roll.fabric_weight_gsm, 430)
        self.assertEqual(self.roll.version, 1)

        # B 重新拉取后再改 → 成功，版本再 +1
        fresh = self.client.get(f"/api/rolls/{self.roll.id}/").data
        resp_retry = self.client.patch(
            f"/api/rolls/{self.roll.id}/",
            {"fabricWeightGsm": 450, "version": fresh["version"]},
            format="json",
        )
        self.assertEqual(resp_retry.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_retry.data["version"], 2)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.fabric_weight_gsm, 450)

    def test_update_without_version_rejected(self):
        self._as(self.worker_a)
        resp = self.client.patch(
            f"/api/rolls/{self.roll.id}/",
            {"fabricWeightGsm": 430},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("version", resp.data)

    def test_after_conflict_resolved_filter_counts_agree(self):
        """冲突解决后再筛 >400：接口条数与库内计数一致。"""
        self._as(self.worker_a)
        self.client.patch(
            f"/api/rolls/{self.roll.id}/",
            {"fabricWeightGsm": 430, "version": 0},
            format="json",
        )
        resp = self.client.get("/api/rolls/?gsmMin=400")
        rows = resp.data["results"] if "results" in resp.data else resp.data
        db_count = ClothRoll.objects.filter(fabric_weight_gsm__gt=400).count()
        self.assertEqual(len(rows), db_count)
        self.assertEqual(len(rows), 1)
