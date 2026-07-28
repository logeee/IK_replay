"""拨动开关全流程编排（无前端、无交互，按步骤直接执行）。

未完成的步骤（YOLO 场景判断/点位识别/成功复核、起手式、新腰部调节、
快速回落）留为 stub：调用即抛 FlowError(NOT_IMPLEMENTED)，流程在该步
中止并打印停在哪。后续逐个把 stub 换成实现即可，编排本身不用再动。

错误码：占位。等正式定义后替换 ErrorCode 的取值即可，接口形状不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .client import ReachClient


class ErrorCode(IntEnum):
    """占位错误码——正式编码待定义，先保证每类失败有独立的码。"""

    OK = 0
    NOT_IMPLEMENTED = 1    # 步骤尚未实现
    PRECONDITION = 2       # 服务不可用 / 未接管 / DDS 断开等前置失败
    ALIGN_FAILED = 3       # 腰部调节超时或不收敛
    MEASURE_FAILED = 4     # 平面/距离测量失败
    YOLO_FAILED = 5        # YOLO 检测失败（场景/点位/复核通用，细分待定）
    IK_FAILED = 6          # IK 不可达或预演碰撞
    EXEC_FAILED = 7        # 真机执行失败
    VERIFY_FAILED = 8      # 拨动复核不通过且重试耗尽
    ABORTED = 9            # 人工急停或外部中断


class FlowError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FlowResult:
    ok: bool
    code: ErrorCode
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


class SwitchFlow:
    """一键开始 → … → 拨动完成 的整条流程。

    可覆写的 stub（子类实现或直接改本文件）：
      detect_scene / choose_opening_pose / apply_opening_pose /
      detect_points / verify_flip / descend_fast / waist_align
    """

    def __init__(self,
                 client: ReachClient | None = None,
                 coarse_tol_deg: float = 0.5,   # 3️⃣ 平面指数粗收敛阈值
                 fine_tol_deg: float = 2.0,     # 5️⃣ 保持阶段阈值
                 dmin: float = 0.4, dmax: float = 1.0,
                 max_flip_rounds: int = 3,      # 拨动失败回到 5️⃣ 的最大轮数
                 align_timeout_s: float = 60.0):
        self.client = client or ReachClient()
        self.coarse_tol_deg = coarse_tol_deg
        self.fine_tol_deg = fine_tol_deg
        self.dmin = dmin
        self.dmax = dmax
        self.max_flip_rounds = max_flip_rounds
        self.align_timeout_s = align_timeout_s

    # ------------------------------------------------------------------ 主流程

    def run(self) -> FlowResult:
        """1️⃣ 一键开始。任何一步失败即返回，携带占位错误码。"""
        t0 = time.monotonic()
        try:
            self._log("═══ 1️⃣ 一键开始 ═══")
            self._preflight()

            self._log("═══ 2️⃣ YOLO 场景判断（远方/就地、是否需要拨动）═══")
            scene = self.detect_scene()
            if not scene.get("need_flip", True):
                return self._done(t0, "无需拨动，流程结束", scene=scene)
            self._log(f"场景: {scene}")

            self._log(f"═══ 3️⃣ 腰部调节：平面指数收进 ±{self.coarse_tol_deg}° ═══")
            self.waist_align(self.coarse_tol_deg)

            self._log("═══ 4️⃣ 测距离 ═══")
            distance_m = self.measure_distance()
            self._log(f"距柜面 {distance_m:.3f} m")

            last_error: FlowError | None = None
            for round_no in range(1, self.max_flip_rounds + 1):
                self._log(f"═══ 5️⃣ 第 {round_no}/{self.max_flip_rounds} 轮 ═══")
                try:
                    pose = self.choose_opening_pose(distance_m)
                    self._log(f"起手式: {pose}")
                    self.apply_opening_pose(pose)

                    self._log(f"腰部调节：收进 ±{self.fine_tol_deg}° 并保持")
                    self.waist_align(self.fine_tol_deg)

                    self._log("YOLO 识别点位")
                    points = self.detect_points()
                    self._log(f"点位: {points}")

                    self._log("IK 执行拨动")
                    self.flip_switch(points)

                    self._log("YOLO 复核拨动结果")
                    if self.verify_flip():
                        self._log("拨动成功 ✔")
                        self._log("═══ 收尾：快速回落 ═══")
                        self.descend_fast(pose)
                        return self._done(t0, "拨动成功", rounds=round_no,
                                          distance_m=distance_m)
                    self._log("拨动未成功，回到 5️⃣ 重试")
                    last_error = FlowError(ErrorCode.VERIFY_FAILED, "复核未通过")
                except FlowError as exc:
                    if exc.code == ErrorCode.NOT_IMPLEMENTED:
                        raise                      # stub 未实现：直接中止，不空转重试
                    self._log(f"本轮失败（{exc.code.name}: {exc.message}），回到 5️⃣")
                    last_error = exc
            raise last_error or FlowError(ErrorCode.VERIFY_FAILED, "重试轮数耗尽")

        except FlowError as exc:
            self._log(f"✘ 流程中止：[{exc.code.name}] {exc.message}")
            return FlowResult(ok=False, code=exc.code, message=exc.message,
                              detail={"elapsed_s": round(time.monotonic() - t0, 1)})

    # ------------------------------------------------------------ 已就绪的步骤

    def _preflight(self) -> None:
        """服务、DDS、手臂接管状态检查。"""
        try:
            st = self.client.status()
        except Exception as exc:
            raise FlowError(ErrorCode.PRECONDITION, f"reach_server 不可达: {exc}")
        self._log(f"服务状态: armed={st.get('armed')} "
                  f"arm_supported={st.get('arm_supported')}")
        if not st.get("arm_supported"):
            raise FlowError(ErrorCode.PRECONDITION, "无真机执行能力（--no-robot 模式？）")
        if not st.get("armed"):
            self._log("未接管手臂，自动接管…")
            res = self.client.arm()
            if not res.get("ok"):
                raise FlowError(ErrorCode.PRECONDITION,
                                f"接管失败: {res.get('error')}")

    def measure_plane(self) -> dict:
        """平面指数（yaw_err_deg）等测量，失败抛 MEASURE_FAILED。"""
        fit = self.client.perpendicular(self.dmin, self.dmax)
        if not fit.get("ok"):
            raise FlowError(ErrorCode.MEASURE_FAILED,
                            f"平面拟合失败: {fit.get('error')}")
        return fit

    def measure_distance(self) -> float:
        return float(self.measure_plane()["distance_m"])

    def waist_align(self, tol_deg: float) -> None:
        """腰部调节：把平面指数收进 ±tol_deg。

        TODO(新腰部调节)：将替换为从 hold_*.jsonl 学到的"打杆式"控制，
        支持任意 tol。当前先用已验证的一键对中占位——它的收敛阈值由
        服务端按手臂姿态自动选（收回 0.35° / 前伸 2.8°），tol 只用于
        结束后的达标校验。
        """
        already = abs(float(self.measure_plane()["yaw_err_deg"]))
        if already <= tol_deg:
            self._log(f"平面指数已在 ±{tol_deg}° 内（{already:.2f}°），跳过")
            return
        res = self.client.align_yaw_start(self.dmin, self.dmax)
        if not res.get("ok"):
            raise FlowError(ErrorCode.ALIGN_FAILED,
                            f"对中启动失败: {res.get('error')}")
        deadline = time.monotonic() + self.align_timeout_s
        while time.monotonic() < deadline:
            time.sleep(1.0)
            fit = self.client.perpendicular(self.dmin, self.dmax)
            align = fit.get("align") or {}
            if not align.get("running"):
                err = abs(float(fit.get("yaw_err_deg") or 999.0)) if fit.get("ok") else None
                self._log(f"对中结束: {align.get('message')}（残差 {err}°）")
                if err is not None and err <= tol_deg:
                    return
                raise FlowError(ErrorCode.ALIGN_FAILED,
                                f"对中结束但残差 {err}° 未达 ±{tol_deg}°")
            self._log(f"对中中… {align.get('message') or ''}")
        self.client.align_yaw_stop()
        raise FlowError(ErrorCode.ALIGN_FAILED, f"对中超时（>{self.align_timeout_s}s）")

    # ------------------------------------------------------- 待实现的步骤（stub）

    def detect_scene(self) -> dict:
        """2️⃣ YOLO：远方还是就地、是否需要拨动。

        TODO：接 YOLO。约定返回 {"need_flip": bool, "range": "near"|"far", ...}
        """
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "YOLO 场景判断未实现")

    def choose_opening_pose(self, distance_m: float) -> dict:
        """5️⃣ 按距离选起手式。

        TODO：距离分档 → 预备关节姿态（可存 reach_sequences，按名字引用）。
        约定返回 {"name": str, ...}。
        """
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "起手式选择未实现")

    def apply_opening_pose(self, pose: dict) -> None:
        """把手臂摆到起手式。

        TODO：走 /sequences/run 或 /hand_move 到位。
        """
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "起手式执行未实现")

    def detect_points(self) -> list[dict]:
        """YOLO 识别开关点位（像素坐标）。

        TODO：接 YOLO。约定返回 [{"u": int, "v": int, "label": str}, ...]。
        """
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "YOLO 点位识别未实现")

    def flip_switch(self, points: list[dict]) -> None:
        """IK 执行拨动。

        TODO：对每个点位 client.pick(u, v) 预演 → client.execute() 执行，
        失败按 IK_FAILED / EXEC_FAILED 抛。
        """
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "IK 拨动执行未实现")

    def verify_flip(self) -> bool:
        """YOLO 复核是否拨动成功。TODO：接 YOLO。"""
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "拨动复核未实现")

    def descend_fast(self, pose: dict | None) -> None:
        """收尾快速回落（插值法直接下来，可统一回"近距离起手式"）。

        TODO：待起手式定型后实现。
        """
        raise FlowError(ErrorCode.NOT_IMPLEMENTED, "快速回落未实现")

    # ---------------------------------------------------------------- 工具

    def _done(self, t0: float, message: str, **detail: Any) -> FlowResult:
        self._log(f"✔ {message}")
        return FlowResult(ok=True, code=ErrorCode.OK, message=message,
                          detail={"elapsed_s": round(time.monotonic() - t0, 1),
                                  **detail})

    @staticmethod
    def _log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] [flow] {msg}", flush=True)
