-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.14 TO 6.3.15
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_assistant_attachments` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `conversation_id` BIGINT NOT NULL,
  `user_id` BIGINT NOT NULL,
  `enterprise_space_id` BIGINT NOT NULL,
  `original_filename` VARCHAR(255) NOT NULL,
  `storage_filename` CHAR(36) NOT NULL,
  `mime_type` VARCHAR(128) NOT NULL,
  `file_size` BIGINT NOT NULL,
  `sha256` CHAR(64) NOT NULL,
  `extraction_status` VARCHAR(32) NOT NULL DEFAULT 'pending',
  `extraction_error` VARCHAR(255) NULL,
  `extracted_text` LONGTEXT NULL,
  `is_deleted` BOOL NOT NULL DEFAULT FALSE,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE INDEX `tbl_ai_assistant_attachments_index_1` (`storage_filename`),
  INDEX `tbl_ai_assistant_attachments_index_2` (`conversation_id`, `is_deleted`, `id`),
  INDEX `tbl_ai_assistant_attachments_index_3` (`user_id`, `enterprise_space_id`, `is_deleted`)
);

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_assistant_report_versions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `conversation_id` BIGINT NOT NULL,
  `user_id` BIGINT NOT NULL,
  `enterprise_space_id` BIGINT NOT NULL,
  `report_type` VARCHAR(64) NOT NULL,
  `report_mode` VARCHAR(32) NOT NULL,
  `content` LONGTEXT NOT NULL,
  `data_sources_json` LONGTEXT NULL,
  `is_deleted` BOOL NOT NULL DEFAULT FALSE,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `tbl_ai_assistant_report_versions_index_1` (`conversation_id`, `is_deleted`, `id`),
  INDEX `tbl_ai_assistant_report_versions_index_2` (`user_id`, `enterprise_space_id`, `is_deleted`)
);

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_assistant_message_attachments` (
  `message_id` BIGINT NOT NULL,
  `attachment_id` BIGINT NOT NULL,
  PRIMARY KEY (`message_id`, `attachment_id`),
  INDEX `tbl_ai_assistant_message_attachments_index_1` (`attachment_id`, `message_id`)
);

ALTER TABLE `myems_system_db`.`tbl_ai_assistant_messages`
  ADD COLUMN `report_version_id` BIGINT NULL AFTER `use_system_data`;

ALTER TABLE `myems_system_db`.`tbl_ai_assistant_audits`
  ADD COLUMN `report_mode` VARCHAR(32) NULL AFTER `model_name`,
  ADD COLUMN `data_sources_json` LONGTEXT NULL AFTER `duration_ms`;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.15', release_date='2026-10-08'
WHERE id=1;

COMMIT;