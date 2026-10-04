from django.db.models import F
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import APIException

from .models import ClothRoll, DipRun, Loft
from .rules import can_mark_roll_cured


class VersionConflict(APIException):
    """客户端读取的版本已过期（他人先改过同一卷）。"""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "该布卷已被他人修改，请刷新架面后重试"
    default_code = "version_conflict"


class LoftSerializer(serializers.ModelSerializer):
    rollCount = serializers.SerializerMethodField()

    class Meta:
        model = Loft
        fields = ("id", "name", "location", "notes", "rollCount", "created_at")
        read_only_fields = ("id", "rollCount", "created_at")

    def get_rollCount(self, obj):
        if hasattr(obj, "roll_count"):
            return obj.roll_count
        return obj.rolls.count()


class ClothRollSerializer(serializers.ModelSerializer):
    loftId = serializers.PrimaryKeyRelatedField(source="loft", queryset=Loft.objects.all())
    rollCode = serializers.CharField(source="roll_code")
    fabricWeightGsm = serializers.IntegerField(source="fabric_weight_gsm", required=False)
    loftName = serializers.CharField(source="loft.name", read_only=True)
    # 乐观锁版本：GET 返回当前版本；更新时必须原样带回，过期则 409。
    version = serializers.IntegerField(min_value=0, required=False)

    class Meta:
        model = ClothRoll
        fields = (
            "id",
            "loftId",
            "loftName",
            "rollCode",
            "status",
            "fabricWeightGsm",
            "notes",
            "version",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "loftName", "created_at", "updated_at")

    def validate(self, attrs):
        loft = attrs.get("loft") or getattr(self.instance, "loft", None)
        roll_code = attrs.get("roll_code") or getattr(self.instance, "roll_code", None)
        if loft and roll_code:
            qs = ClothRoll.objects.filter(loft=loft, roll_code=roll_code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"rollCode": "同一帆布间卷号必须唯一"})

        new_status = attrs.get("status")
        if new_status == ClothRoll.STATUS_CURED:
            roll = self.instance
            if roll is None:
                raise serializers.ValidationError(
                    {"status": "新建布卷不能直接设为已固化"}
                )
            # 合并未提交字段到临时视角：用当前实例校验
            ok, msg = can_mark_roll_cured(roll)
            if not ok:
                raise serializers.ValidationError({"status": msg})

        if self.instance is not None and "version" not in attrs:
            raise serializers.ValidationError(
                {"version": "更新布卷必须携带读取时的 version"}
            )
        return attrs

    def create(self, validated_data):
        # 新建一律从 0 开始，忽略客户端传入的 version
        validated_data.pop("version", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        expected_version = validated_data.pop("version")
        if expected_version != instance.version:
            raise VersionConflict()

        # updated_at 是 auto_now，queryset.update 不会自动刷新，手动带上。
        validated_data["updated_at"] = timezone.now()
        updated = (
            ClothRoll.objects.filter(pk=instance.pk, version=expected_version)
            .update(version=F("version") + 1, **validated_data)
        )
        if not updated:
            # 读版本与校验之间被他人抢先提交：两个浸胶工的交叉修改只成一版。
            raise VersionConflict()
        instance.refresh_from_db()
        return instance


class DipRunSerializer(serializers.ModelSerializer):
    rollId = serializers.PrimaryKeyRelatedField(
        source="roll", queryset=ClothRoll.objects.all()
    )
    startedAt = serializers.DateTimeField(source="started_at")
    resinPct = serializers.DecimalField(source="resin_pct", max_digits=5, decimal_places=2)
    cureHours = serializers.DecimalField(
        source="cure_hours",
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    rollCode = serializers.CharField(source="roll.roll_code", read_only=True)
    loftName = serializers.CharField(source="roll.loft.name", read_only=True)

    class Meta:
        model = DipRun
        fields = (
            "id",
            "rollId",
            "rollCode",
            "loftName",
            "startedAt",
            "resinPct",
            "cureHours",
            "notes",
            "created_at",
        )
        read_only_fields = ("id", "rollCode", "loftName", "created_at")
