// Script principal du site.
// Il s'exécute une fois que le DOM de la page est entièrement chargé.
document.addEventListener('DOMContentLoaded', () => {
    // On récupère l'élément HTML qui affiche l'année du footer.
    const yearElement = document.getElementById('current-year');

    // Si l'élément existe, on remplace son texte par l'année actuelle.
    // Exemple : 2026, 2027, etc.
    if (yearElement) {
        yearElement.textContent = new Date().getFullYear();
    }
});
