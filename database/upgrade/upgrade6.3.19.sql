-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.18 TO 6.3.19
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

ALTER TABLE `myems_system_db`.`tbl_ai_assistant_report_versions`
  ADD COLUMN `parent_report_version_id` BIGINT NULL AFTER `template_version_id`,
  ADD COLUMN `report_context_json` LONGTEXT NULL AFTER `data_sources_json`,
  ADD INDEX `tbl_ai_assistant_report_versions_index_3` (`conversation_id`, `parent_report_version_id`, `id`);

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.19', release_date='2026-10-10'
WHERE id=1;

COMMIT;