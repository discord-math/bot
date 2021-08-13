CREATE VIEW tickets.mod_queues AS
	SELECT tkt.id AS id
		FROM tickets.mods mod
			INNER JOIN tickets.tickets tkt ON mod.modid = tkt.modid AND tkt.id =
				(SELECT t.id
					FROM tickets.tickets t
					WHERE mod.modid = t.modid AND stage <> 'COMMENTED'
					ORDER BY t.id LIMIT 1
				);
