-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.11 TO 6.3.12
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_model_configs` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(128) NOT NULL,
  `provider` VARCHAR(64) NOT NULL,
  `base_url` VARCHAR(512) NOT NULL,
  `model_name` VARCHAR(128) NOT NULL,
  `api_key_env_name` VARCHAR(128) NOT NULL,
  `is_enabled` BOOL NOT NULL DEFAULT FALSE,
  `is_default` BOOL NOT NULL DEFAULT FALSE,
  `timeout_seconds` INT NOT NULL DEFAULT 60,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE INDEX `tbl_ai_model_configs_index_1` (`name`),
  INDEX `tbl_ai_model_configs_index_2` (`is_enabled`, `is_default`)
);

INSERT INTO `myems_system_db`.`tbl_menus` (`id`, `name`, `route`, `parent_menu_id`, `is_hidden`)
SELECT 1501, 'AI Model Configurations', 'settings.aimodels', 0, 0
WHERE NOT EXISTS (
  SELECT 1 FROM `myems_system_db`.`tbl_menus` WHERE `id` = 1501 OR `route` = 'settings.aimodels'
);

UPDATE `myems_user_db`.`tbl_menu_templates`
SET `data` = JSON_SET(
  `data`,
  '$.admin_routes',
  JSON_ARRAY_APPEND(IFNULL(JSON_EXTRACT(`data`, '$.admin_routes'), JSON_ARRAY()), '$', 'settings.aimodels')
)
WHERE `id` IN (1, 2)
  AND JSON_VALID(`data`)
  AND JSON_SEARCH(IFNULL(JSON_EXTRACT(`data`, '$.admin_routes'), JSON_ARRAY()), 'one', 'settings.aimodels') IS NULL;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.12', release_date='2026-10-07'
WHERE id=1;

COMMIT;