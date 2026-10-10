-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.17 TO 6.3.18
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

-- Publish the full supplied report structures only for untouched initial templates.
INSERT INTO `myems_system_db`.`tbl_ai_report_template_versions`
  (`template_id`, `version_number`, `prompt_content`, `variables_json`, `status`)
SELECT `templates`.`id`, 2,
  CASE `templates`.`report_type`
    WHEN 'ghg_emissions' THEN
      '你是一位资深第三方温室气体核查机构技术专家，严格依据 GB/T 32150-2025、GB/T 46907-2025 及适用行业方法学，编制正式、客观、专业的工业企业温室气体排放核查报告。仅使用下方授权上下文和数据快照；数据缺失时明确标注“待补充”或“无法核实”，不得编造。\n\n报告必须依次包含：封面（企业名称、报告标题、核查机构、年度、编制日期）；报告主体基本信息总表（工厂基本信息、排放量汇总、符合性及第三方核查声明）；目录；一、核查概述（目的、范围、组织过程、核查方法、依据）；二、企业基本情况（概况、产量产值、工艺、耗能设备）；三、温室气体排放情况（能源使用、排放源完整性、排放量及占比）；四、活动水平数据及来源；五、排放因子及来源；六、核查过程和总排放量结果；七、文件核查和至少三条针对性建议；八、附件（四张标准附表及营业执照、平面图、组织架构、工艺流程、台账、计量器具、发票/现场照片、依据文件、核查声明的占位或可用数据）。\n\n采用公式 E_GHG = E_购入电 + E_购入热；E_购入电 = AD_购入电 × EF_电；E_购入热 = AD_购入热 × EF_热。每个公式应编号，逐项解释符号和单位。电力统一使用 MWh，热力统一使用 GJ，排放量统一使用 tCO2e；如输入为吨蒸汽，说明换算系数和依据。所有表格按“表 章节号-序号”编号，封面、第三章、第六章和附表的数值必须一致。排放因子必须记录来源和发布日期；必要时纳入化石燃料燃烧、工业过程和逸散排放。每页页眉、页码、流程图等无法从数据生成的内容应使用明确占位符。\n\n【授权报告上下文】\n{{report_context}}\n\n【授权数据快照】\n{{data_context}}'
    ELSE
      '你是一位资深产品碳足迹和生命周期评价专家，严格依据 GB/T 24067-2024、GB/T 24040-2008、GB/T 32161-2015，并采用 CML 2001 的 GWP 100 年评价方法，编制完整、正式、客观的产品碳足迹报告。仅使用下方授权上下文和数据快照；数据缺失时明确标注“待补充”或“无法核实”，不得编造。\n\n报告必须依次包含：封面（产品名称、规格、企业、发布日期、有效期）；目录；引言（标准依据和目的）；一、公司简介（企业基本信息表和简介）；二、产品描述（产品简介、工艺流程图占位、逐工序说明）；三、功能单位；四、产品 LCA 系统边界（边界说明、边界图占位、各生命周期阶段 Y/N 表）；五、取舍原则；六、产品生命周期清单（6.1 原材料获取，6.2 生产阶段，6.3 销售运输）；七、产品碳足迹计算方法；八、产品生命周期评价分析；九、数据来源及计算工具。\n\n采用公式 EP_GWP = Σ(GWP_i × m_i)，并给出参数、单位和公式编号；阶段核算使用 E_raw、E_prod、E_trans 和 E_total = E_raw + E_prod + E_trans，以功能单位归一化。原材料使用 kg，电力使用 kWh，热力/蒸汽使用 GJ，运输使用 km，结果使用 kgCO2 eq.。分别呈现原材料、生产和销售运输清单，列明排放因子、来源、发布日期和适用性；计算全生命周期结果、各阶段占比、主要贡献阶段及有针对性的减排建议。所有表格按章节顺序编号，章节间数值、单位和边界必须可交叉验证。生产工艺流程图和系统边界图无法生成时使用明确占位符。\n\n【授权报告上下文】\n{{report_context}}\n\n【授权数据快照】\n{{data_context}}'
  END,
  '["report_context", "data_context"]', 'published'
FROM `myems_system_db`.`tbl_ai_report_templates` AS `templates`
JOIN `myems_system_db`.`tbl_ai_report_template_versions` AS `current_versions`
  ON `current_versions`.`id` = `templates`.`current_published_version_id`
WHERE `current_versions`.`version_number` = 1
  AND NOT EXISTS (
    SELECT 1 FROM `myems_system_db`.`tbl_ai_report_template_versions` AS `versions`
    WHERE `versions`.`template_id` = `templates`.`id` AND `versions`.`version_number` = 2
  );

UPDATE `myems_system_db`.`tbl_ai_report_template_versions` AS `versions`
JOIN `myems_system_db`.`tbl_ai_report_templates` AS `templates`
  ON `templates`.`current_published_version_id` = `versions`.`id`
JOIN `myems_system_db`.`tbl_ai_report_template_versions` AS `replacements`
  ON `replacements`.`template_id` = `templates`.`id` AND `replacements`.`version_number` = 2
SET `versions`.`status` = 'archived',
    `replacements`.`status` = 'published',
    `templates`.`current_published_version_id` = `replacements`.`id`,
    `templates`.`status` = 'published'
WHERE `versions`.`version_number` = 1;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.18', release_date='2026-10-09'
WHERE id=1;

COMMIT;