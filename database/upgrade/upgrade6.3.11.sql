-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.10 TO 6.3.11
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_assistant_conversations` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `enterprise_space_id` BIGINT NOT NULL,
  `title` VARCHAR(128) NOT NULL,
  `is_deleted` BOOL NOT NULL DEFAULT FALSE,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `tbl_ai_assistant_conversations_index_1` (`user_id`, `enterprise_space_id`, `is_deleted`, `updated_datetime_utc`)
);

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_assistant_messages` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `conversation_id` BIGINT NOT NULL,
  `role` VARCHAR(16) NOT NULL,
  `content` LONGTEXT NOT NULL,
  `model_name` VARCHAR(128) NULL,
  `use_system_data` BOOL NOT NULL DEFAULT FALSE,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `tbl_ai_assistant_messages_index_1` (`conversation_id`, `id`)
);

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_assistant_audits` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `conversation_id` BIGINT NULL,
  `user_id` BIGINT NOT NULL,
  `enterprise_space_id` BIGINT NOT NULL,
  `provider` VARCHAR(64) NOT NULL,
  `model_name` VARCHAR(128) NULL,
  `status` VARCHAR(32) NOT NULL,
  `error_code` VARCHAR(128) NULL,
  `duration_ms` BIGINT NULL,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `tbl_ai_assistant_audits_index_1` (`enterprise_space_id`, `user_id`, `created_datetime_utc`)
);

INSERT INTO `myems_system_db`.`tbl_menus` (`id`, `name`, `route`, `parent_menu_id`, `is_hidden`)
SELECT 1500, 'AI Assistant', '/ai-assistant', NULL, 0
WHERE NOT EXISTS (
  SELECT 1 FROM `myems_system_db`.`tbl_menus` WHERE `id` = 1500 OR `route` = '/ai-assistant'
);

UPDATE `myems_user_db`.`tbl_menu_templates`
SET `data` = JSON_SET(
  `data`,
  '$.web_routes',
  JSON_ARRAY_APPEND(IFNULL(JSON_EXTRACT(`data`, '$.web_routes'), JSON_ARRAY()), '$', '/ai-assistant')
)
WHERE `id` IN (1, 2, 3)
  AND JSON_VALID(`data`)
  AND JSON_SEARCH(IFNULL(JSON_EXTRACT(`data`, '$.web_routes'), JSON_ARRAY()), 'one', '/ai-assistant') IS NULL;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.11', release_date='2026-10-07'
WHERE id=1;

COMMIT;