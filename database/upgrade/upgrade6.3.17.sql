-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.16 TO 6.3.17
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

INSERT INTO `myems_system_db`.`tbl_menus` (`id`, `name`, `route`, `parent_menu_id`, `is_hidden`)
SELECT 1502, 'AI Report Templates', 'settings.aireporttemplates', 0, 0
WHERE NOT EXISTS (
  SELECT 1 FROM `myems_system_db`.`tbl_menus` WHERE `id` = 1502 OR `route` = 'settings.aireporttemplates'
);

UPDATE `myems_user_db`.`tbl_menu_templates`
SET `data` = JSON_SET(
  `data`,
  '$.admin_routes',
  JSON_ARRAY_APPEND(IFNULL(JSON_EXTRACT(`data`, '$.admin_routes'), JSON_ARRAY()), '$', 'settings.aireporttemplates')
)
WHERE `id` IN (1, 2)
  AND JSON_VALID(`data`)
  AND JSON_SEARCH(IFNULL(JSON_EXTRACT(`data`, '$.admin_routes'), JSON_ARRAY()), 'one', 'settings.aireporttemplates') IS NULL;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.17', release_date='2026-10-09'
WHERE id=1;

COMMIT;