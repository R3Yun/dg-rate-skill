# -*- coding: utf-8 -*-
"""文本/聊天/纯文本运价解析器 — P0优先级
支持船运价格.txt这类纯文本格式，以及群聊里粘贴的运价消息。
"""
import os
import re
from typing import List, Tuple
from .base import BaseRateParser, register_parser, NormalizedRateEntry, DGSurcharge


class TextPriceParser(BaseRateParser):
    name = "text_chat"
    source_type = "text_chat"
    carrier_names = [
        "SITC", "TSL", "YML", "MSC", "MSK", "EMC", "CMA", "ONE", "HMM", "ZIM",
        "OOCL", "KMTC", "RCL", "IAL", "WHL", "SINOKOR", "HEUNG-A", "HPL",
        "COSCO", "HAS", "ANL", "MCC", "MAERSK", "CMA CGM",
    ]

    def can_parse(self, content: str, filename: str="") -> Tuple[float, str]:
        score = 0.0
        reason = []
        if not content: return (0.0, "")
        # 文件名特征
        fn = filename.lower()
        if "txt" in fn or "价格" in filename or "船运" in filename:
            score += 0.3; reason.append("文件名匹配文本价格")
        # 内容特征：多组港口+数字
        if re.search(r"[A-Z]{5}|[\u4e00-\u9fa5]{2,4}\s*(?:→|->|-|到)\s*[A-Z]{5}|[\u4e00-\u9fa5]{2,4}", content):
            if re.findall(r"\d{2,4}\s*/\s*\d{2,4}(?:\s*/\s*\d{2,4})?", content):
                score += 0.4; reason.append("多组 价格/价格/价格 格式")
        # DG标记
        if re.search(r"DG\s*\d", content, re.I):
            score += 0.2; reason.append("含DG附加费")
        # 有效期
        if re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", content):
            score += 0.2; reason.append("含日期有效期")
        # 不含Excel/HTML特征
        if "<html" in content.lower() or "<?xml" in content:
            return (0.0, "不是纯文本")
        return (min(score, 1.0), "; ".join(reason))

    def parse(self, content: str, filename: str="") -> List[NormalizedRateEntry]:
        results = []
        if not content: return results
        lines = content.splitlines()
        # 先提取全局信息：船公司、有效期、币种
        carrier = ""
        valid_from, valid_to = "", ""
        currency = "USD"
        for line in lines[:10]:
            for name in ["SITC","TSL","YML","MSC","MSK","EMC","CMA","ONE","HMM","ZIM","OOCL","KMTC","RCL","IAL","WHL","SINOKOR","HEUNG-A","兴亚","海丰","德翔","阳明","地中海","马士基","长荣","达飞","中远"]:
                if name in line and not carrier:
                    carrier = self._map_carrier(name)
            vf, vt = self.parse_date(line)
            if vf and not valid_from: valid_from = vf
            if vt and not valid_to: valid_to = vt
            if "RMB" in line or "人民币" in line: currency = "RMB"

        # 按行解析港口+价格
        current_carrier = carrier
        current_valid_from, current_valid_to = valid_from, valid_to

        for line in lines:
            line_carrier = self._extract_carrier_context(line)
            if line_carrier:
                current_carrier = line_carrier
            vf, vt = self.parse_date(line)
            if vf:
                current_valid_from = vf
            if vt:
                current_valid_to = vt
            if re.search(r"(^|[\s:：])同上([\s。,.，；;]|$)", line, re.I):
                continue
            entry = self._parse_line(line, filename)
            if entry:
                if not entry.carrier: entry.carrier = current_carrier or carrier
                if not entry.valid_from: entry.valid_from = current_valid_from or valid_from
                if not entry.valid_to: entry.valid_to = current_valid_to or valid_to
                entry.currency = currency
                results.append(entry)
        return results

    def _extract_carrier_context(self, line: str) -> str:
        """Extract carrier context from headings like '船公司：OOCL'."""
        if not line:
            return ""
        text = line.strip()
        m = re.search(r"(?:船公司|船司|Carrier)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\- ]{1,20})", text, re.I)
        if m:
            value = re.split(r"\s{2,}|[，,;；。]", m.group(1).strip())[0].upper()
            return self._map_carrier(value)
        upper = text.upper()
        for name in self.carrier_names:
            if re.search(rf"(?<![A-Z0-9]){re.escape(name)}(?![A-Z0-9])", upper):
                return self._map_carrier(name)
        return ""

    def _map_carrier(self, name: str) -> str:
        m = {"海丰":"SITC","SITC":"SITC","兴亚":"SINOKOR","SINOKOR":"SINOKOR","HEUNG-A":"SINOKOR",
             "德翔":"TSL","TSL":"TSL","阳明":"YML","YML":"YML","MSC":"MSC","马士基":"MSK","MSK":"MSK",
             "长荣":"EMC","EMC":"EMC","达飞":"CMA","CMA":"CMA","ONE":"ONE","中远":"OOCL","OOCL":"OOCL",
             "KMTC":"KMTC","RCL":"RCL","WHL":"WHL","赫伯罗特":"HLC","HPL":"HLC","HMM":"HMM","ZIM":"ZIM"}
        return m.get(name, name)

    def _parse_line(self, line: str, filename: str):
        """解析单行运价，支持两种格式：
           (1) 中文/英文港名 USD price/price  (如 BUSAN USD 140/280, 日偏 USD 250/500)
           (2) 港名 price/price                (如 曼谷 450/800/800)
           排除附加费行（BAF/LSS/AFR/ENS/OWS/CAF/CIC等）
        """
        if not line.strip() or len(line.strip()) < 5: return None
        if re.match(r"^[\s\-=]+$", line): return None
        stripped = line.strip()
        # 排除明显不是港口价格行（说明/标题/附加费）
        skip_keywords = ["价格调整", "长期要货", "另加", "附加费", "危险品附加费",
                        "小箱控制", "目的港可申请", "默认", "可申请", "单票"]
        if any(k in stripped for k in skip_keywords) and not re.match(r"^[A-Z\u4e00-\u9fa5]", stripped):
            return None
        # 提取「USD price/price」价格组（核心特征）
        usd_price = re.search(r"USD\s+(\d{2,4}(?:\.\d+)?)\s*/\s*(\d{2,4}(?:\.\d+)?)(?:\s*/\s*(\d{2,4}(?:\.\d+)?))?", stripped, re.I)
        # 或者纯「数字/数字」价格（紧跟在港名后面）
        bare_price = re.search(r"(?<=[A-Z\u4e00-\u9fa5\s])\s+(\d{2,4}(?:\.\d+)?)\s*/\s*(\d{2,4}(?:\.\d+)?)(?:\s*/\s*(\d{2,4}(?:\.\d+)?))?", stripped)
        first = usd_price or bare_price
        if not first: return None
        # 附加费行识别：价格在附加费关键词后（如BAF USD 50/100, LSS USD 200/400）
        before_text = stripped[:first.start()].upper()
        surcharge_words = ["DG","BAF","LSS","AFR","CAF","CIC","ENS","AMS","OWS","PCS","RS","THC","DOC","SEAL","VGM","WRS","EBS","CIC","PSS","GRI"]
        # 如果价格前的文字以附加费词结尾，这是附加费行，跳过
        if re.search(r"(?:USD\s*)?(?:" + "|".join(surcharge_words) + r")\s*$", before_text):
            return None
        entry = self.new_entry(filename)
        try:
            entry.of_20 = float(first.group(1))
            entry.of_40 = float(first.group(2))
            if first.group(3): entry.of_40hq = float(first.group(3))
        except: return None
        # 过滤明显非海运费的数字（DG附加费等小数字）
        if entry.of_20 and entry.of_20 < 80 and not usd_price:
            # 可能是附加费，跳过
            return None
        # 提取港口名（价格前的文字）
        before = stripped[:first.start()].strip()
        before = re.sub(r"^\d+[\.、\s]+", "", before).strip()  # 去掉序号
        before = re.sub(r"USD\s*$", "", before, flags=re.I).strip()
        before = re.sub(r"[（(][^）)]*[）)]\s*$", "", before).strip()  # 去括号备注
        # 识别船公司前缀
        carrier = ""
        for cname in ["SITC","YML","MSC","EMC","TSL","KMTC","RCL","WHL","SINOKOR","OOCL","ONE","CMA","ZIM","HMM","HPL","IAL","HEUNG-A","COSCO","TSL","HAS","HMM","ANL","MCC","RCL","IAL"]:
            if before.upper().startswith(cname):
                carrier = cname
                before = before[len(cname):].strip()
                break
        entry.carrier = carrier
        # 港口名：优先全大写英文港名
        en_match = re.search(r"([A-Z][A-Z\s\-]{2,}[A-Z])\s*$", before)
        if en_match:
            port_name = en_match.group(1).strip()
        else:
            # 中文港名取最后
            tokens = before.split()
            port_name = tokens[-1] if tokens else before
        port_name = port_name.strip(" -:：，,。.")
        if not port_name or len(port_name) < 2: return None
        entry.pod = self.normalize_port(port_name)
        entry.pod_name = port_name
        # 起运港：默认上海；如果文本提到其他港口（如SHEKOU/蛇口），用那个
        if "SHEKOU" in stripped.upper() or "蛇口" in stripped:
            entry.pol = "CNSZN"; entry.pol_name = "蛇口"
        elif "YANTIAN" in stripped.upper() or "盐田" in stripped:
            entry.pol = "CNYTN"; entry.pol_name = "盐田"
        elif "NINGBO" in stripped.upper() or "宁波" in stripped:
            entry.pol = "CNNGB"; entry.pol_name = "宁波"
        else:
            entry.pol = "CNSHA"; entry.pol_name = "上海"
        # DG解析
        dgs = self.parse_dg_remark(stripped)
        entry.dg_surcharges = dgs
        # Pure surcharge lines are skipped before entry creation; lines that carry
        # both base ocean freight and DG surcharge must be kept as rate entries.
        # 航程/直航
        tt = re.search(r"(\d+)\s*天", stripped)
        if tt: entry.tt_days = int(tt.group(1))
        if "直" in stripped or "直达" in stripped: entry.direct = "Y"
        if "中转" in stripped: entry.direct = "T"
        entry.raw_excerpt = stripped
        return entry
register_parser(TextPriceParser())
