#!/usr/bin/env python3
"""Run one policy-Pod -> clean-renderer -> SlideForge Reward PPT control episode."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_ROOT = PROJECT_ROOT / "data/example-ppt-bundles/bundles"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "data/openreward_ppt_k8s/artifacts"
DEFAULT_TASK_ID = "q0210_0001_bbed2931e2616ef3_open_create_structured"

_DECK_SOURCE = r'''const pptxgen = require("pptxgenjs");
const pptx = new pptxgen();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "Research Ops OpenReward Control";
pptx.company = "Research Ops";
pptx.subject = "临床输液护理技术进展与操作规范";
pptx.title = "临床输液护理技术进展与操作规范";
pptx.lang = "zh-CN";
pptx.theme = {
  headFontFace: "Noto Sans CJK SC",
  bodyFontFace: "Noto Sans CJK SC",
  lang: "zh-CN"
};

const C = {
  ink: "17324D",
  teal: "0F766E",
  mint: "DDF4EF",
  coral: "E76F51",
  amber: "F4A261",
  sand: "F7F3EA",
  white: "FFFFFF",
  muted: "5D7184",
  pale: "EEF4F6",
  redPale: "FBE9E7",
  green: "2A9D8F"
};
const FONT = "Noto Sans CJK SC";
const makeShadow = () => ({
  type: "outer", color: "000000", blur: 3, offset: 1, angle: 135, opacity: 0.12
});

function addFooter(slide, page, dark = false) {
  slide.addText("临床输液护理 · 科室业务学习", {
    x: 0.65, y: 7.12, w: 4.2, h: 0.18,
    fontFace: FONT, fontSize: 8.5, color: dark ? C.mint : C.muted, margin: 0
  });
  slide.addText(String(page).padStart(2, "0"), {
    x: 12.08, y: 7.08, w: 0.55, h: 0.22,
    fontFace: FONT, fontSize: 9, bold: true, color: dark ? C.mint : C.teal,
    align: "right", margin: 0
  });
}

function baseSlide(title, section, page, dark = false) {
  const slide = pptx.addSlide();
  slide.background = { color: dark ? C.ink : C.sand };
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.65, y: 0.48, w: 1.65, h: 0.34,
    fill: { color: dark ? C.mint : C.ink },
    line: { color: dark ? C.mint : C.ink },
    rectRadius: 0.04
  });
  slide.addText(section, {
    x: 0.78, y: 0.56, w: 1.38, h: 0.16,
    fontFace: FONT, fontSize: 9.5, bold: true,
    color: dark ? C.ink : C.white, align: "center", margin: 0
  });
  slide.addText(title, {
    x: 0.65, y: 0.98, w: 11.8, h: 0.52,
    fontFace: FONT, fontSize: 28, bold: true,
    color: dark ? C.white : C.ink, margin: 0
  });
  addFooter(slide, page, dark);
  return slide;
}

function addCard(slide, x, y, w, h, title, body, accent = C.teal) {
  slide.addShape(pptx.ShapeType.roundRect, {
    x, y, w, h,
    fill: { color: C.white },
    line: { color: "D4E0E5", width: 1 },
    rectRadius: 0.05,
    shadow: makeShadow()
  });
  slide.addShape(pptx.ShapeType.ellipse, {
    x: x + 0.28, y: y + 0.28, w: 0.42, h: 0.42,
    fill: { color: accent }, line: { color: accent }
  });
  slide.addText(title, {
    x: x + 0.85, y: y + 0.26, w: w - 1.12, h: 0.34,
    fontFace: FONT, fontSize: 17, bold: true, color: C.ink, margin: 0
  });
  slide.addText(body, {
    x: x + 0.3, y: y + 0.86, w: w - 0.6, h: h - 1.12,
    fontFace: FONT, fontSize: 13, color: C.muted,
    breakLine: false, valign: "mid", margin: 0.04
  });
}

function addBullets(slide, items, x, y, w, h, size = 15, color = C.ink) {
  const runs = items.map((item, index) => ({
    text: item,
    options: { bullet: true, breakLine: index < items.length - 1, paraSpaceAfterPt: 12 }
  }));
  slide.addText(runs, {
    x, y, w, h, fontFace: FONT, fontSize: size,
    color, breakLine: false, valign: "top", margin: 0.08
  });
}

// 01 — title
{
  const slide = pptx.addSlide();
  slide.background = { color: C.ink };
  slide.addShape(pptx.ShapeType.ellipse, {
    x: 9.25, y: -0.5, w: 4.7, h: 4.7,
    fill: { color: C.teal, transparency: 15 },
    line: { color: C.teal, transparency: 100 }
  });
  slide.addShape(pptx.ShapeType.ellipse, {
    x: 10.35, y: 3.55, w: 2.45, h: 2.45,
    fill: { color: C.coral, transparency: 8 },
    line: { color: C.coral, transparency: 100 }
  });
  slide.addText("临床输液护理技术进展\n与操作规范", {
    x: 0.8, y: 1.48, w: 8.6, h: 1.65,
    fontFace: FONT, fontSize: 32, bold: true,
    color: C.white, breakLine: false, margin: 0
  });
  slide.addText("面向临床护理人员的科室业务学习", {
    x: 0.84, y: 3.48, w: 6.8, h: 0.38,
    fontFace: FONT, fontSize: 17, color: C.mint, margin: 0
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.82, y: 5.25, w: 4.55, h: 0.62,
    fill: { color: C.white, transparency: 88 },
    line: { color: C.white, transparency: 75 }, rectRadius: 0.04
  });
  slide.addText("识别风险 · 规范操作 · 及时处置", {
    x: 1.08, y: 5.44, w: 4.05, h: 0.24,
    fontFace: FONT, fontSize: 13, bold: true, color: C.white,
    align: "center", margin: 0
  });
  addFooter(slide, 1, true);
}

// 02 — learning map
{
  const slide = baseSlide("本次学习地图", "课程导航", 2);
  const labels = [
    ["01", "技术演进"], ["02", "通路选择"], ["03", "穿刺改良"],
    ["04", "固定与拔针"], ["05", "并发症处置"], ["06", "质量闭环"]
  ];
  labels.forEach((item, index) => {
    const x = 0.72 + (index % 3) * 4.15;
    const y = 1.85 + Math.floor(index / 3) * 2.12;
    slide.addShape(pptx.ShapeType.roundRect, {
      x, y, w: 3.72, h: 1.48,
      fill: { color: index === 4 ? C.redPale : C.white },
      line: { color: index === 4 ? C.coral : "D4E0E5", width: 1 },
      rectRadius: 0.05, shadow: makeShadow()
    });
    slide.addText(item[0], {
      x: x + 0.28, y: y + 0.3, w: 0.72, h: 0.48,
      fontFace: FONT, fontSize: 24, bold: true,
      color: index === 4 ? C.coral : C.teal, margin: 0
    });
    slide.addText(item[1], {
      x: x + 1.18, y: y + 0.4, w: 2.1, h: 0.36,
      fontFace: FONT, fontSize: 18, bold: true, color: C.ink, margin: 0
    });
  });
}

// 03 — history timeline
{
  const slide = baseSlide("从“能输”走向“精准、安全、可追踪”", "技术演进", 3);
  const nodes = [
    ["早期探索", "循环理论与静脉补液尝试"],
    ["密闭系统", "一次性器材降低交叉污染"],
    ["导管时代", "外周与中心静脉装置分层"],
    ["智能输注", "泵控、报警与信息化记录"],
    ["专业治理", "通路团队与质量指标闭环"]
  ];
  slide.addShape(pptx.ShapeType.rect, {
    x: 1.05, y: 3.42, w: 11.15, h: 0.08,
    fill: { color: "B8D8D3" }, line: { color: "B8D8D3" }
  });
  nodes.forEach((item, index) => {
    const x = 0.78 + index * 2.45;
    slide.addShape(pptx.ShapeType.ellipse, {
      x: x + 0.75, y: 3.13, w: 0.62, h: 0.62,
      fill: { color: index === 4 ? C.coral : C.teal },
      line: { color: C.white, width: 2 }
    });
    slide.addText(item[0], {
      x, y: index % 2 === 0 ? 2.05 : 4.18, w: 2.15, h: 0.36,
      fontFace: FONT, fontSize: 16, bold: true, color: C.ink,
      align: "center", margin: 0
    });
    slide.addText(item[1], {
      x, y: index % 2 === 0 ? 2.46 : 4.62, w: 2.15, h: 0.72,
      fontFace: FONT, fontSize: 11.5, color: C.muted,
      align: "center", valign: "mid", margin: 0.03
    });
  });
}

// 04 — principles
{
  const slide = baseSlide("现代输液护理的四个决策支点", "核心理念", 4);
  addCard(
    slide, 0.75, 1.8, 5.75, 2.02,
    "治疗匹配", "依据疗程、药液性质与速度需求选择通路。", C.teal
  );
  addCard(
    slide, 6.8, 1.8, 5.75, 2.02,
    "血管保护", "优先满足治疗需求的最小管径与最低侵入等级。", C.green
  );
  addCard(
    slide, 0.75, 4.08, 5.75, 2.02,
    "感染预防", "把手卫生、无菌屏障与接头管理落实到每次操作。", C.amber
  );
  addCard(
    slide, 6.8, 4.08, 5.75, 2.02,
    "动态评估", "每日判断通路必要性，并追踪疼痛、红肿与功能变化。", C.coral
  );
}

// 05 — device matrix
{
  const slide = baseSlide("常用血管通路：按需求选择，不按习惯选择", "通路工具", 5);
  slide.addTable([
    ["装置", "典型治疗需求", "主要优势", "关键关注"],
    ["头皮钢针", "单次、短时且非刺激性治疗", "操作直接", "不可留置；活动受限"],
    ["外周静脉导管", "常规短疗程治疗", "减少重复穿刺", "评估渗出、静脉炎与固定"],
    ["中线导管", "中等疗程、外周可耐受药液", "留置时间较长", "尖端位置与适用药液边界"],
    ["PICC / CVC", "长期或需中心静脉通路的治疗", "满足复杂治疗", "专业置管、维护与感染预防"]
  ], {
    x: 0.72, y: 1.78, w: 11.85, h: 4.68,
    colW: [1.7, 3.25, 2.75, 4.15],
    rowH: [0.62, 0.88, 0.88, 0.88, 0.88],
    border: { pt: 1, color: "C8D8DE" },
    fill: { color: C.white },
    color: C.ink, fontFace: FONT, fontSize: 11.5,
    margin: 0.08,
    bold: false,
    autoFit: false,
    valign: "mid"
  });
  slide.addShape(pptx.ShapeType.rect, {
    x: 0.72, y: 1.78, w: 11.85, h: 0.62,
    fill: { color: C.ink }, line: { color: C.ink }
  });
  ["装置", "典型治疗需求", "主要优势", "关键关注"].forEach((text, index) => {
    const xs = [0.84, 2.54, 5.79, 8.54];
    const ws = [1.45, 3.0, 2.5, 3.8];
    slide.addText(text, {
      x: xs[index], y: 1.98, w: ws[index], h: 0.22,
      fontFace: FONT, fontSize: 12, bold: true,
      color: C.white, align: "center", margin: 0
    });
  });
}

// 06 — assessment
{
  const slide = baseSlide("穿刺前评估：先回答三个问题", "操作准备", 6);
  const cols = [
    ["患者", "意识与配合\n既往通路史\n出血与皮肤风险", C.teal],
    ["治疗", "疗程与频次\n药液性质\n速度和监测要求", C.amber],
    ["血管", "走向与弹性\n局部皮肤状态\n远端优先与替代方案", C.coral]
  ];
  cols.forEach((item, index) => {
    const x = 0.78 + index * 4.15;
    slide.addShape(pptx.ShapeType.roundRect, {
      x, y: 1.9, w: 3.72, h: 4.35,
      fill: { color: C.white }, line: { color: "D4E0E5" },
      rectRadius: 0.05, shadow: makeShadow()
    });
    slide.addShape(pptx.ShapeType.ellipse, {
      x: x + 1.22, y: 2.28, w: 1.28, h: 1.28,
      fill: { color: item[2] }, line: { color: item[2] }
    });
    slide.addText(item[0], {
      x: x + 1.36, y: 2.69, w: 1.0, h: 0.3,
      fontFace: FONT, fontSize: 20, bold: true,
      color: C.white, align: "center", margin: 0
    });
    slide.addText(item[1], {
      x: x + 0.46, y: 4.0, w: 2.8, h: 1.55,
      fontFace: FONT, fontSize: 15, color: C.ink,
      align: "center", breakLine: false, valign: "mid", margin: 0.03
    });
  });
}

// 07 — aseptic process
{
  const slide = baseSlide("无菌与穿刺准备：五步闭环", "操作准备", 7);
  const steps = [
    ["1", "核对与解释"], ["2", "手卫生"], ["3", "备齐并检查器材"],
    ["4", "皮肤消毒并待干"], ["5", "建立通路并确认功能"]
  ];
  steps.forEach((item, index) => {
    const x = 0.72 + index * 2.5;
    slide.addShape(pptx.ShapeType.roundRect, {
      x, y: 2.35, w: 2.08, h: 2.72,
      fill: { color: index === 4 ? C.mint : C.white },
      line: { color: index === 4 ? C.teal : "D4E0E5" },
      rectRadius: 0.05, shadow: makeShadow()
    });
    slide.addText(item[0], {
      x: x + 0.65, y: 2.72, w: 0.78, h: 0.7,
      fontFace: FONT, fontSize: 28, bold: true,
      color: index === 4 ? C.coral : C.teal, align: "center", margin: 0
    });
    slide.addText(item[1], {
      x: x + 0.27, y: 3.75, w: 1.54, h: 0.78,
      fontFace: FONT, fontSize: 14, bold: true, color: C.ink,
      align: "center", valign: "mid", margin: 0.02
    });
  });
  slide.addText("任何环节被污染，都应立即停止并重新建立无菌条件。", {
    x: 2.4, y: 5.65, w: 8.55, h: 0.42,
    fontFace: FONT, fontSize: 14, bold: true,
    color: C.coral, align: "center", margin: 0
  });
}

// 08 — puncture technique
{
  const slide = baseSlide("穿刺改良：让每一步都有观察依据", "穿刺技术", 8);
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.75, y: 1.78, w: 5.55, h: 4.78,
    fill: { color: C.ink }, line: { color: C.ink }, rectRadius: 0.05
  });
  slide.addText("操作节奏", {
    x: 1.08, y: 2.12, w: 2.2, h: 0.4,
    fontFace: FONT, fontSize: 20, bold: true, color: C.mint, margin: 0
  });
  addBullets(slide, [
    "稳定皮肤与血管，避免反复探查",
    "见回血后降低角度，再小幅推进",
    "送管与退针芯动作分离、轻柔",
    "连接后先确认通畅和局部反应"
  ], 1.02, 2.82, 4.85, 2.8, 15, C.white);
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 6.62, y: 1.78, w: 5.92, h: 4.78,
    fill: { color: C.white }, line: { color: "D4E0E5" },
    rectRadius: 0.05, shadow: makeShadow()
  });
  slide.addText("改良工具", {
    x: 6.98, y: 2.12, w: 2.2, h: 0.4,
    fontFace: FONT, fontSize: 20, bold: true, color: C.teal, margin: 0
  });
  addBullets(slide, [
    "热敷或体位调整：改善可见与可触性",
    "小角度进针：用于浅表、脆弱血管",
    "超声引导：用于困难血管并由受训人员实施",
    "失败后及时升级通路策略，避免同点重复尝试"
  ], 6.93, 2.82, 5.0, 2.8, 14, C.ink);
}

// 09 — special populations
{
  const slide = baseSlide("特殊人群：技术参数之外，更要调整策略", "穿刺技术", 9);
  const items = [
    ["老年患者", "轻柔固定皮肤；避开脆弱、迂曲区域", C.teal],
    ["儿童患者", "减少等待与反复尝试；强化舒适和陪伴", C.amber],
    ["肥胖或水肿", "触诊与超声辅助；谨慎判断深度", C.green],
    ["凝血风险", "评估出血风险；拔针后延长观察", C.coral]
  ];
  items.forEach((item, index) => {
    const x = 0.78 + (index % 2) * 6.05;
    const y = 1.82 + Math.floor(index / 2) * 2.35;
    addCard(slide, x, y, 5.72, 2.02, item[0], item[1], item[2]);
  });
}

// 10 — fixation
{
  const slide = baseSlide("固定质量：既要稳，也要看得见", "固定与维护", 10);
  slide.addShape(pptx.ShapeType.ellipse, {
    x: 5.15, y: 2.38, w: 3.0, h: 3.0,
    fill: { color: C.teal }, line: { color: C.white, width: 3 },
    shadow: makeShadow()
  });
  slide.addText("固定\n质量", {
    x: 5.8, y: 3.22, w: 1.7, h: 0.9,
    fontFace: FONT, fontSize: 24, bold: true,
    color: C.white, align: "center", valign: "mid", margin: 0
  });
  const checks = [
    [0.8, 1.92, "牢固", "无牵拉、移位或折叠"],
    [8.8, 1.92, "可视", "穿刺点与周围皮肤清晰"],
    [0.8, 4.85, "舒适", "关节活动与循环不受限"],
    [8.8, 4.85, "整洁", "敷料干燥、边缘完整"]
  ];
  checks.forEach((item, index) => {
    addCard(slide, item[0], item[1], 3.3, 1.45, item[2], item[3],
      index === 3 ? C.coral : C.teal);
  });
}

// 11 — removal
{
  const slide = baseSlide("拔针不是终点：规范完成止血与观察", "固定与维护", 11);
  const steps = [
    ["停止输注", "关闭调节装置并核对通路"],
    ["松解固定", "顺皮肤与毛发生长方向移除"],
    ["平稳拔出", "沿导管方向快速、稳定完成"],
    ["有效按压", "覆盖皮肤与血管穿刺点"],
    ["复查记录", "确认止血、导管完整与皮肤状态"]
  ];
  steps.forEach((item, index) => {
    const y = 1.72 + index * 0.96;
    slide.addShape(pptx.ShapeType.roundRect, {
      x: 1.0, y, w: 1.65, h: 0.7,
      fill: { color: index === 4 ? C.coral : C.teal },
      line: { color: index === 4 ? C.coral : C.teal }, rectRadius: 0.04
    });
    slide.addText(item[0], {
      x: 1.2, y: y + 0.21, w: 1.25, h: 0.22,
      fontFace: FONT, fontSize: 13, bold: true,
      color: C.white, align: "center", margin: 0
    });
    slide.addShape(pptx.ShapeType.roundRect, {
      x: 2.92, y, w: 8.9, h: 0.7,
      fill: { color: C.white }, line: { color: "D4E0E5" }, rectRadius: 0.04
    });
    slide.addText(item[1], {
      x: 3.25, y: y + 0.19, w: 8.2, h: 0.26,
      fontFace: FONT, fontSize: 14, color: C.ink, margin: 0
    });
  });
}

// 12 — infiltration vs extravasation
{
  const slide = baseSlide("先区分：渗出与外渗不是同一风险等级", "并发症", 12);
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.78, y: 1.8, w: 5.7, h: 4.7,
    fill: { color: C.pale }, line: { color: "BBD5DE" }, rectRadius: 0.05
  });
  slide.addText("渗出  Infiltration", {
    x: 1.13, y: 2.18, w: 4.85, h: 0.42,
    fontFace: FONT, fontSize: 21, bold: true, color: C.teal, margin: 0
  });
  addBullets(slide, [
    "非发疱性液体进入周围组织",
    "常见肿胀、凉感、滴速改变",
    "重点是停止输注、评估范围与持续观察"
  ], 1.05, 2.9, 4.95, 2.45, 14, C.ink);
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 6.82, y: 1.8, w: 5.7, h: 4.7,
    fill: { color: C.redPale }, line: { color: "E8B7AE" }, rectRadius: 0.05
  });
  slide.addText("外渗  Extravasation", {
    x: 7.17, y: 2.18, w: 4.85, h: 0.42,
    fontFace: FONT, fontSize: 21, bold: true, color: C.coral, margin: 0
  });
  addBullets(slide, [
    "刺激性或发疱性药液进入组织",
    "可能出现疼痛、水疱或组织损伤",
    "按药物专项预案升级处置并报告"
  ], 7.08, 2.9, 4.95, 2.45, 14, C.ink);
}

// 13 — extravasation response
{
  const slide = baseSlide("疑似外渗：按预案迅速完成六件事", "并发症", 13);
  const steps = [
    "立即停止输注，不冲洗原通路",
    "保留通路并按药物预案评估是否回抽",
    "评估疼痛、皮肤改变与累及范围",
    "通知责任医生或专科团队并启动专项预案",
    "按药物要求采取局部措施，避免经验性处理",
    "标记、拍照、记录并持续随访"
  ];
  steps.forEach((text, index) => {
    const col = index % 2;
    const row = Math.floor(index / 2);
    const x = 0.78 + col * 6.05;
    const y = 1.75 + row * 1.55;
    slide.addShape(pptx.ShapeType.roundRect, {
      x, y, w: 5.72, h: 1.2,
      fill: { color: index < 2 ? C.redPale : C.white },
      line: { color: index < 2 ? C.coral : "D4E0E5" },
      rectRadius: 0.05, shadow: makeShadow()
    });
    slide.addText(String(index + 1).padStart(2, "0"), {
      x: x + 0.28, y: y + 0.37, w: 0.64, h: 0.3,
      fontFace: FONT, fontSize: 17, bold: true,
      color: index < 2 ? C.coral : C.teal, margin: 0
    });
    slide.addText(text, {
      x: x + 1.08, y: y + 0.27, w: 4.25, h: 0.58,
      fontFace: FONT, fontSize: 13.5, color: C.ink,
      valign: "mid", margin: 0.02
    });
  });
}

// 14 — phlebitis
{
  const slide = baseSlide("静脉炎：从原因入手做预防", "并发症", 14);
  const causes = [
    ["机械性", "导管与血管壁摩擦\n固定不稳或管径不匹配", C.teal],
    ["化学性", "药液浓度、pH 或渗透压刺激\n输注速度不合适", C.amber],
    ["感染性", "无菌操作或接头管理缺陷\n局部或全身感染征象", C.coral]
  ];
  causes.forEach((item, index) => {
    const x = 0.78 + index * 4.15;
    addCard(slide, x, 1.82, 3.72, 2.28, item[0], item[1], item[2]);
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.78, y: 4.48, w: 12.02, h: 1.65,
    fill: { color: C.ink }, line: { color: C.ink }, rectRadius: 0.05
  });
  slide.addText(
    "发现疼痛、红肿、条索样改变或分泌物时：" +
      "停止使用该通路，评估分级，按机构流程处置并记录。", {
    x: 1.22, y: 4.9, w: 11.1, h: 0.72,
    fontFace: FONT, fontSize: 15, bold: true, color: C.white,
    align: "center", valign: "mid", margin: 0.03
    }
  );
}

// 15 — other complications
{
  const slide = baseSlide("其他常见风险：识别信号与第一反应", "并发症", 15);
  const rows = [
    ["导管堵塞", "滴速异常、不能冲管", "先查扭折与夹闭；禁止暴力冲管"],
    ["疑似感染", "局部红肿、分泌物或全身症状", "停止使用并按感染流程评估、采样和报告"],
    ["导管脱出", "外露长度变化、固定松动", "停止使用；评估位置与完整性，不盲目回送"]
  ];
  rows.forEach((item, index) => {
    const y = 1.78 + index * 1.52;
    slide.addShape(pptx.ShapeType.roundRect, {
      x: 0.78, y, w: 12.0, h: 1.18,
      fill: { color: index === 1 ? C.redPale : C.white },
      line: { color: index === 1 ? C.coral : "D4E0E5" },
      rectRadius: 0.05, shadow: makeShadow()
    });
    slide.addText(item[0], {
      x: 1.05, y: y + 0.39, w: 1.5, h: 0.26,
      fontFace: FONT, fontSize: 16, bold: true,
      color: index === 1 ? C.coral : C.teal, margin: 0
    });
    slide.addText(item[1], {
      x: 2.75, y: y + 0.28, w: 3.45, h: 0.48,
      fontFace: FONT, fontSize: 13, color: C.muted,
      valign: "mid", margin: 0.02
    });
    slide.addText(item[2], {
      x: 6.45, y: y + 0.28, w: 5.85, h: 0.48,
      fontFace: FONT, fontSize: 13, bold: true, color: C.ink,
      valign: "mid", margin: 0.02
    });
  });
}

// 16 — surveillance
{
  const slide = baseSlide("巡视不是“看一眼”：形成可追溯记录", "质量管理", 16);
  const metrics = [
    ["通畅", "滴速与泵报警"],
    ["局部", "疼痛、红肿、渗漏"],
    ["固定", "敷料与外露长度"],
    ["患者", "主诉与全身反应"]
  ];
  metrics.forEach((item, index) => {
    const x = 0.78 + index * 3.0;
    slide.addShape(pptx.ShapeType.roundRect, {
      x, y: 1.85, w: 2.7, h: 2.18,
      fill: { color: index === 3 ? C.mint : C.white },
      line: { color: index === 3 ? C.teal : "D4E0E5" },
      rectRadius: 0.05, shadow: makeShadow()
    });
    slide.addText(item[0], {
      x: x + 0.35, y: 2.28, w: 2.0, h: 0.45,
      fontFace: FONT, fontSize: 22, bold: true,
      color: index === 3 ? C.coral : C.teal, align: "center", margin: 0
    });
    slide.addText(item[1], {
      x: x + 0.35, y: 3.08, w: 2.0, h: 0.48,
      fontFace: FONT, fontSize: 13, color: C.ink,
      align: "center", valign: "mid", margin: 0.02
    });
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 1.52, y: 4.55, w: 10.25, h: 1.15,
    fill: { color: C.ink }, line: { color: C.ink }, rectRadius: 0.05
  });
  slide.addText("异常信号 → 立即评估 → 必要时停用通路 → 升级处置 → 完整记录与交接", {
    x: 1.95, y: 4.94, w: 9.4, h: 0.3,
    fontFace: FONT, fontSize: 15, bold: true,
    color: C.white, align: "center", margin: 0
  });
}

// 17 — scenario
{
  const slide = baseSlide("床旁情境：患者诉穿刺点灼痛，你先做什么？", "情境练习", 17);
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.78, y: 1.8, w: 4.15, h: 4.8,
    fill: { color: C.ink }, line: { color: C.ink }, rectRadius: 0.05
  });
  slide.addText("情境", {
    x: 1.13, y: 2.18, w: 1.2, h: 0.38,
    fontFace: FONT, fontSize: 20, bold: true, color: C.mint, margin: 0
  });
  slide.addText("患者输液中诉局部灼痛，敷贴边缘可见轻度肿胀，泵未报警。", {
    x: 1.12, y: 2.92, w: 3.45, h: 1.6,
    fontFace: FONT, fontSize: 18, bold: true, color: C.white,
    valign: "mid", margin: 0.04
  });
  slide.addText("先停、再看、后分级", {
    x: 1.15, y: 5.35, w: 3.35, h: 0.36,
    fontFace: FONT, fontSize: 15, bold: true,
    color: C.amber, align: "center", margin: 0
  });
  const options = [
    ["A", "加快滴速观察是否通畅"],
    ["B", "立即停止输注并评估局部情况"],
    ["C", "按摩肿胀区域促进吸收"],
    ["D", "直接拔针，不做记录"]
  ];
  options.forEach((item, index) => {
    const y = 1.86 + index * 1.15;
    slide.addShape(pptx.ShapeType.roundRect, {
      x: 5.32, y, w: 7.08, h: 0.9,
      fill: { color: index === 1 ? C.mint : C.white },
      line: { color: index === 1 ? C.teal : "D4E0E5" },
      rectRadius: 0.04, shadow: makeShadow()
    });
    slide.addText(item[0], {
      x: 5.65, y: y + 0.27, w: 0.48, h: 0.25,
      fontFace: FONT, fontSize: 16, bold: true,
      color: index === 1 ? C.coral : C.teal, margin: 0
    });
    slide.addText(item[1], {
      x: 6.3, y: y + 0.24, w: 5.65, h: 0.3,
      fontFace: FONT, fontSize: 14, color: C.ink, margin: 0
    });
  });
}

// 18 — close
{
  const slide = baseSlide("把安全落实到每一次接触通路", "总结", 18, true);
  const items = [
    ["选择", "让治疗需求、患者风险与装置能力匹配"],
    ["操作", "让无菌、固定、巡视成为可观察的标准动作"],
    ["处置", "让异常被早发现、早停止、早升级、可追溯"]
  ];
  items.forEach((item, index) => {
    const x = 0.8 + index * 4.18;
    slide.addShape(pptx.ShapeType.roundRect, {
      x, y: 2.0, w: 3.78, h: 2.55,
      fill: { color: C.white, transparency: 5 },
      line: { color: index === 2 ? C.coral : C.mint, width: 1.5 },
      rectRadius: 0.05
    });
    slide.addText(item[0], {
      x: x + 0.35, y: 2.42, w: 3.08, h: 0.52,
      fontFace: FONT, fontSize: 24, bold: true,
      color: index === 2 ? C.coral : C.teal,
      align: "center", margin: 0
    });
    slide.addText(item[1], {
      x: x + 0.24, y: 3.25, w: 3.30, h: 0.76,
      fontFace: FONT, fontSize: 13.5, color: C.ink,
      align: "center", valign: "mid", margin: 0.02
    });
  });
  slide.addText(
    "具体操作、药物外渗处理和通路维护应遵循本机构最新制度、" +
      "药品说明书与专科团队指导。", {
    x: 1.35, y: 5.35, w: 10.65, h: 0.55,
    fontFace: FONT, fontSize: 13, color: C.mint,
    align: "center", valign: "mid", margin: 0.02
    }
  );
}

pptx.writeFile({ fileName: "/workspace/slides.pptx" });
'''


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _output(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if not isinstance(output, dict):
        raise RuntimeError("PPT runtime response omitted output")
    return dict(output)


def _text_block(output: dict[str, Any]) -> str:
    blocks = output.get("blocks")
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in blocks
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _safe_error(exc: Exception) -> str:
    """Keep diagnostics useful without allowing an accidental credential echo."""

    value = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(?i)sk-[A-Za-z0-9_-]+", "[REDACTED]", value)[:2000]


def _post(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(path, json=payload)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"PPT runtime returned non-object JSON for {path}")
    return dict(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-url",
        default=os.environ.get("ORS_PPT_RUNTIME_URL", "http://openreward-ppt-runtime:9103"),
    )
    parser.add_argument("--token-env", default="ORS_PPT_RUNTIME_TOKEN")
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)
    parser.add_argument("--timeout-seconds", type=float, default=7000.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = str(os.environ.get(args.token_env, "")).strip()
    if not token:
        raise RuntimeError(f"missing runtime credential reference: {args.token_env}")
    bundle_root = args.bundle_root.expanduser().resolve(strict=True)
    task_path = bundle_root / args.task_id / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    if not isinstance(task, dict) or task.get("task_id") != args.task_id:
        raise RuntimeError("PPT control task bundle is invalid")
    artifact_root = args.artifact_root.expanduser().resolve(strict=True)
    output_path = args.output or (
        artifact_root / "control" / f"{os.environ.get('HOSTNAME', 'local')}.json"
    )
    output_path = output_path.expanduser().resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(args.timeout_seconds, connect=30.0)
    session_id = ""
    created: dict[str, Any] = {}
    written: dict[str, Any] = {}
    generated: dict[str, Any] = {}
    generation_metadata: dict[str, Any] = {}
    inspection: dict[str, Any] = {}
    verified: dict[str, Any] = {}
    failure: str | None = None
    with httpx.Client(
        base_url=args.runtime_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        trust_env=False,
    ) as client:
        try:
            health = client.get("/healthz")
            health.raise_for_status()
            health_payload = health.json()
            if (
                health_payload.get("status") != "ok"
                or health_payload.get("rubric_ready") is not True
            ):
                raise RuntimeError("PPT runtime is not ready for a real control episode")
            created = _post(
                client,
                "/v1/presentations",
                {"task_id": args.task_id, "task_spec": task},
            )
            session_id = str(created.get("session_id") or "")
            if not session_id:
                raise RuntimeError("PPT runtime did not return a session_id")
            written = _output(
                _post(
                    client,
                    f"/v1/presentations/{session_id}/files/write",
                    {"input": {"path": "/workspace/create.js", "content": _DECK_SOURCE}},
                )
            )
            generated = _output(
                _post(
                    client,
                    f"/v1/presentations/{session_id}/exec",
                    {"input": {"command": "node --version && node create.js"}},
                )
            )
            generation_metadata = _mapping(generated.get("metadata"))
            if generation_metadata.get("exit_code") != 0:
                raise RuntimeError(f"PPT source execution failed: {_text_block(generated)}")
            inspected = _output(
                _post(
                    client,
                    "/v1/presentations/read",
                    {
                        "session_id": session_id,
                        "input": {"path": "/workspace/slides.pptx"},
                    },
                )
            )
            inspection = json.loads(_text_block(inspected))
            if inspection.get("slides") != 18 or inspection.get("text_runs", 0) < 80:
                raise RuntimeError("generated PPT failed the control content gate")
            verified = _output(
                _post(
                    client,
                    "/v1/presentations/verify",
                    {
                        "session_id": session_id,
                        "input": {"path": "/workspace/slides.pptx"},
                    },
                )
            )
        except Exception as exc:
            failure = _safe_error(exc)
        finally:
            if session_id:
                try:
                    client.delete(f"/v1/presentations/{session_id}")
                except Exception as exc:
                    failure = failure or _safe_error(exc)

    metadata = _mapping(verified.get("metadata"))
    if failure is None and verified.get("finished") is not True:
        failure = "PPT control did not reach a terminal result"
    if failure is None and (
        metadata.get("structural_pass") is not True
        or metadata.get("clean_renderer") is not True
    ):
        failure = "PPT control did not pass the clean renderer"
    if failure is None and (metadata.get("usable") is not True or verified.get("reward") is None):
        failure = "PPT control did not return a usable real Reward score"
    evidence = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": args.task_id,
        "runtime_url": args.runtime_url,
        "session_id": session_id,
        "runtime": {
            "image_id": created.get("image_id"),
            "rubric_ready": created.get("rubric_ready"),
        },
        "write": _mapping(written.get("metadata")),
        "generation": {
            "metadata": generation_metadata,
            "stdout": _text_block(generated),
        },
        "inspection": inspection,
        "result": {
            "reward": verified.get("reward"),
            "finished": verified.get("finished"),
            "metadata": metadata,
        },
    }
    if failure is not None:
        evidence["error"] = failure
    output_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ready": failure is None,
        "task_id": args.task_id,
        "reward": verified.get("reward"),
        "usable": metadata.get("usable"),
        "slides": metadata.get("slides") or inspection.get("slides"),
        "artifact_id": metadata.get("artifact_id"),
        "evidence": str(output_path),
    }
    if failure is not None:
        summary["error"] = failure
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if failure is not None:
        raise RuntimeError(failure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
