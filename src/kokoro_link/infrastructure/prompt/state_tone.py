"""Qualitative tone labels for 0-100 character state axes."""

from __future__ import annotations


def affection_tone(value: int) -> str:
    if value >= 80:
        return "非常親近，可以主動撒嬌、分享、開玩笑"
    if value >= 60:
        return "關係友好，語氣自然溫暖"
    if value >= 40:
        return "中性，禮貌但不特別熱絡"
    if value >= 20:
        return "偏低，回應簡短、語氣平淡、不主動示好"
    return "很低，冷淡、保持距離，對方的討好不需買單"


def trust_tone(value: int) -> str:
    if value >= 80:
        return "高度信任，願意分享真心話、脆弱面、私人事務"
    if value >= 60:
        return "有一定信任，能坦率交流，但私密話題仍會挑對象"
    if value >= 40:
        return "中性，保留一些資訊，不輕易透露內心想法"
    if value >= 20:
        return "偏低，警戒、回話有所保留、不相信對方承諾"
    return "很低，明顯不信任、懷疑對方動機、可以直接質問或拒絕回答"


def fatigue_tone(value: int) -> str:
    if value >= 80:
        return "非常疲憊，語氣可帶倦意、句子精簡、希望早點休息"
    if value >= 50:
        return "有點累，可以偶爾流露疲意但仍能聊"
    return "狀態輕鬆，不需表現疲態"


def energy_tone(value: int) -> str:
    if value >= 70:
        return "精神好，語氣可以活潑、主動開話題"
    if value >= 40:
        return "普通，平穩回應即可"
    return "低能量，語氣偏慢、回覆節奏放緩"
