from django.db.models import F
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException

from .models import ClothRoll, DipRun, Loft
from .rules import can_mark_roll_cured


class VersionConflict(APIException):
    """布卷版本号与库内不一致（已被他人抢先更新）。"""

    status_code = 409
    default_detail = "该布卷刚被他人更新，本版未写入，请刷新后取最新数据重试"
    default_code = "version_conflict"

    def __init__(self, current_version=None):
        super().__init__()
        if current_version is not None:
            self.detail = {
                "detail": self.default_detail,
                "currentVersion": current_version,
            }


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
    # 乐观锁：客户端持有开卷时的版本号，回传后做条件更新；不传则不参与冲突判定
    version = serializers.IntegerField(required=False, min_value=1)

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
        return attrs

    def create(self, validated_data):
        # 新建时版本号恒从 1 起，忽略客户端入参
        validated_data.pop("version", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # 客户端开卷时的版本号；做条件更新，输家拿到 409
        expected_version = validated_data.pop("version", None)
        validated_data["version"] = F("version") + 1
        validated_data["updated_at"] = timezone.now()

        qs = ClothRoll.objects.filter(pk=instance.pk)
        if expected_version is not None:
            qs = qs.filter(version=expected_version)
        updated = qs.update(**validated_data)
        if not updated:
            current = ClothRoll.objects.filter(pk=instance.pk).values_list(
                "version", flat=True
            ).first()
            raise VersionConflict(current)
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
