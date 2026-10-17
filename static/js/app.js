(() => {
    const sidebar = document.getElementById("app-sidebar");
    const toggle = document.getElementById("sidebar-toggle");
    const closeButton = document.getElementById("sidebar-close");
    const backdrop = document.getElementById("sidebar-backdrop");

    if (!sidebar || !toggle || !backdrop) {
        return;
    }

    function setSidebarOpen(isOpen) {
        sidebar.classList.toggle("is-open", isOpen);
        backdrop.classList.toggle("is-visible", isOpen);
        toggle.setAttribute("aria-expanded", String(isOpen));
        document.body.classList.toggle("sidebar-open", isOpen);
    }

    toggle.addEventListener("click", () => {
        setSidebarOpen(!sidebar.classList.contains("is-open"));
    });

    backdrop.addEventListener("click", () => setSidebarOpen(false));

    if (closeButton) {
        closeButton.addEventListener("click", () => setSidebarOpen(false));
    }

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            setSidebarOpen(false);
        }
    });
})();
