"""InBody 体测照片 AI 识别 — 使用 Vision LLM 从 InBody 屏幕照片中提取数据。

支持：
  - InBody 身体成分页（体重、脂肪量、去脂体重、骨质、肌肉量、蛋白质、身体水分）
  - InBody 身体参数页（体脂率、内脏脂肪、腰臀脂肪比、健壮指数、BMI 等）
  - InBody 调节建议 + 身体评分
  - 自动合并多张图片的数据

通过 OpenRouter API 调用 Vision 模型（支持 GPT-4o、Claude 等）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from engine import llm_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """你是一个 InBody 体测数据提取专家。请从这些 InBody 体测机屏幕照片中提取所有能识别到的数据。

请以 JSON 格式返回，字段名使用以下英文 key（只返回你能识别到的字段，无法识别的不要猜测）：

```
{
  "date": "YYYY-MM-DD",          // 测量日期（从屏幕顶部或图片识别）
  "weight_kg": 63.3,             // 体重
  "body_fat_pct": 12.2,          // 体脂率 (%)
  "fat_mass_kg": 7.7,            // 脂肪量
  "lean_body_mass_kg": 55.6,     // 去脂体重
  "skeletal_muscle_kg": 30.6,    // 骨骼肌
  "muscle_mass_kg": 51.8,        // 肌肉量
  "protein_kg": 11.5,            // 蛋白质
  "bone_mass_kg": 3.8,           // 骨质
  "body_water_kg": 40.3,         // 身体水分 (kg)
  "body_water_pct": null,        // 身体水分比例 (%)
  "bmi": 21.9,                   // BMI
  "visceral_fat_level": 2,       // 内脏脂肪等级
  "waist_hip_ratio": 0.76,       // 腰臀脂肪比
  "fitness_index": 19.2,         // 健壮指数
  "mineral_kg": 1.4,             // 钙质/无机盐
  "bmr_kcal": 1571,              // 基础代谢 (kcal)
  "tdee_kcal": 2514,             // 总能量消耗 (kcal)
  "body_age": 35.7,              // 身体年龄
  "body_score": 83,              // 身体评分
  "body_type": "匀称",           // 体型分析（如：匀称、运动员、健壮 等）
  "fat_adjust_kg": 0.0,          // 脂肪调节建议 (kg)
  "muscle_adjust_kg": 1.4,       // 肌肉调节建议 (kg)
  "weight_adjust_kg": 1.4        // 体重调节建议 (kg)
}
```

注意：
1. 数值请保留原始精度（如 12.2 不要四舍五入为 12）
2. 调节建议中的 +/- 号要正确转为正负数
3. 内脏脂肪是"等级"不是百分比
4. 骨质和钙质是同一个值
5. 如果有多张图片，合并所有识别到的数据

只返回 JSON，不要其他文字。"""


def extract_inbody_data(images: List[bytes]) -> Dict[str, Any]:
    """从 InBody 照片中提取体测数据。

    Args:
        images: 一组 InBody 屏幕照片的二进制数据

    Returns:
        提取到的数据字典，可直接传给 upsert_body_comp()

    Raises:
        ValueError: 如果无法识别或解析
    """
    logger.info("Calling Vision LLM to extract InBody data from %d image(s)...", len(images))

    text = llm_client.vision_completion(
        images=images,
        prompt=_EXTRACTION_PROMPT,
        max_tokens=2000,
    )

    # Parse JSON
    data = llm_client.extract_json(text, expect_array=False)

    # Add source
    data["source"] = "InBody"

    # Clean up: remove null values
    data = {k: v for k, v in data.items() if v is not None}

    logger.info("Extracted InBody data: date=%s, weight=%s, score=%s",
                data.get("date"), data.get("weight_kg"), data.get("body_score"))

    return data
