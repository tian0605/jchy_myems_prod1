-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.15 TO 6.3.16
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_report_templates` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(255) NOT NULL,
  `report_type` VARCHAR(64) NOT NULL,
  `report_mode` VARCHAR(32) NOT NULL,
  `current_published_version_id` BIGINT NULL,
  `status` VARCHAR(32) NOT NULL DEFAULT 'draft',
  `is_deleted` BOOL NOT NULL DEFAULT FALSE,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE INDEX `tbl_ai_report_templates_index_1` (`report_type`, `report_mode`),
  INDEX `tbl_ai_report_templates_index_2` (`status`, `is_deleted`, `id`)
);

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_report_template_versions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `template_id` BIGINT NOT NULL,
  `version_number` INT NOT NULL,
  `prompt_content` LONGTEXT NOT NULL,
  `variables_json` VARCHAR(1024) NOT NULL,
  `status` VARCHAR(32) NOT NULL DEFAULT 'draft',
  `created_by_user_id` BIGINT NULL,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE INDEX `tbl_ai_report_template_versions_index_1` (`template_id`, `version_number`),
  INDEX `tbl_ai_report_template_versions_index_2` (`template_id`, `status`, `id`)
);

ALTER TABLE `myems_system_db`.`tbl_ai_assistant_report_versions`
  ADD COLUMN `template_version_id` BIGINT NULL AFTER `report_mode`;

INSERT INTO `myems_system_db`.`tbl_ai_report_templates`
  (`name`, `report_type`, `report_mode`, `status`)
VALUES
  ('温室气体排放核查报告（真实数据）', 'ghg_emissions', 'real_data', 'published'),
  ('温室气体排放核查报告（估算草稿）', 'ghg_emissions', 'estimate_draft', 'published'),
  ('产品碳足迹报告（真实数据）', 'product_carbon_footprint', 'real_data', 'published'),
  ('产品碳足迹报告（估算草稿）', 'product_carbon_footprint', 'estimate_draft', 'published')
ON DUPLICATE KEY UPDATE `name` = `name`;

INSERT INTO `myems_system_db`.`tbl_ai_report_template_versions`
  (`template_id`, `version_number`, `prompt_content`, `variables_json`, `status`)
SELECT `id`, 1,
  CASE `report_type`
    WHEN 'ghg_emissions' THEN
      'You are a greenhouse gas verification specialist. Prepare a formal industrial greenhouse gas emissions verification report aligned with GB/T 32150-2025 and GB/T 46907-2025. Use the supplied authorized report context and data snapshot only. Include: cover information, verification overview and scope, organizational and operational boundaries, activity data and sources, emission factors and sources, calculation methodology and formulas, emissions results and contribution analysis, data-quality findings, limitations, recommendations, verification statement, and appendices. Keep all values, units, periods, and sources internally consistent. Use professional objective language. {{report_context}} {{data_context}}'
    ELSE
      'You are a product carbon footprint and life-cycle assessment specialist. Prepare a formal product carbon footprint report aligned with GB/T 24067-2024, GB/T 24040-2008, GB/T 32161-2015, and the CML 2001 GWP 100 method. Use the supplied authorized report context and data snapshot only. Include: cover information, company and product description, functional unit, system boundary, cut-off criteria, life-cycle inventory by raw materials/production/transport, emission-factor sources, calculation methodology and formulas, life-cycle impact results, contribution analysis, limitations, reduction opportunities, and data sources. Keep all values, units, periods, and sources internally consistent. Use professional objective language. {{report_context}} {{data_context}}'
  END,
  '["report_context", "data_context"]', 'published'
FROM `myems_system_db`.`tbl_ai_report_templates`
WHERE NOT EXISTS (
  SELECT 1 FROM `myems_system_db`.`tbl_ai_report_template_versions` AS versions
  WHERE versions.template_id = `tbl_ai_report_templates`.`id`
);

UPDATE `myems_system_db`.`tbl_ai_report_templates` AS templates
JOIN `myems_system_db`.`tbl_ai_report_template_versions` AS versions
  ON versions.template_id = templates.id AND versions.version_number = 1
SET templates.current_published_version_id = versions.id
WHERE templates.current_published_version_id IS NULL;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.16', release_date='2026-10-09'
WHERE id=1;

COMMIT;